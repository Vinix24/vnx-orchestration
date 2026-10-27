"""tests/test_vnx_guard_check.py — `vnx guard check` and the `vnx doctor` check built on it.

Plan t0-guard-gebruikersniveau, § "User-level registration", deliverable G2. A tmp ``HOME`` holds
the accounts, the registry ``~/.vnx/projects.json`` and ``~/.vnx-system/current``; the projects
are tmp directories whose settings follow the documented rules of ``hook_coverage`` (a direct
entry needs an existing path, a PreToolUse matcher must match the tool, an unreadable file is
``unknown``). A registered project is also trusted in every account's global config
(``~/.claude.json``, ``<account>/.claude.json``), where Claude Code records trust: a
``hook_coverage`` that reads trust (#2079) then answers as one that does not. Nothing here
touches the real ``~/.claude``, ``~/.claude-*`` or ``~/.vnx``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT))

import guard_check as gc
import guard_install as gi
import hook_coverage as hc

GUARD = hc.GUARD_ENTRY
ALL_EVENTS = ("UserPromptSubmit", "PreToolUse", "Stop")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    hooks = path / ".vnx-system" / "current" / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "hooks" / hc.SHIM_NAME, hooks / hc.SHIM_NAME)
    (hooks / GUARD).write_text("# guard\n", encoding="utf-8")
    return path


def _account(home: Path, name: str = ".claude", installed: bool = True) -> Path:
    account = home / name
    account.mkdir(exist_ok=True)
    (account / "settings.json").write_text("{}\n", encoding="utf-8")
    if installed:
        assert gi.write_settings(account / "settings.json").status == "written"
    return account


def _project(root: Path, name: str, *, t0: bool = True, project_id: str = "consumer") -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    if project_id:
        (project / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    if t0:
        (project / ".claude" / "terminals" / "T0").mkdir(parents=True)
        (project / ".claude" / "terminals" / "T0" / "CLAUDE.md").write_text("# T0\n", encoding="utf-8")
    return project


def _settings(project: Path, hooks: dict, name: str = "settings.json") -> None:
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / name).write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _direct(project: Path) -> dict:
    script = project / "scripts" / "hooks" / GUARD
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# guard\n", encoding="utf-8")
    return {"type": "command", "command": f"python3 {script}"}


def _trust_file(home: Path, account: str) -> Path:
    """Where Claude Code records trust for an account: ``~/.claude.json`` for ``~/.claude``
    (no CLAUDE_CONFIG_DIR), ``<account>/.claude.json`` for every other one."""
    return home / ".claude.json" if account == ".claude" else home / account / ".claude.json"


def _trust(home: Path, account: str, trusted: dict) -> None:
    """Record, per project path, whether the account accepted the trust dialog for it."""
    records = {str(p.resolve()): {"hasTrustDialogAccepted": value} for p, value in trusted.items()}
    _trust_file(home, account).write_text(json.dumps({"projects": records}), encoding="utf-8")


def _register(home: Path, *projects: Path, trust: bool = True) -> None:
    (home / ".vnx").mkdir(exist_ok=True)
    entries = [{"project_id": p.name, "path": str(p)} for p in projects]
    (home / ".vnx" / "projects.json").write_text(json.dumps({"projects": entries}), encoding="utf-8")
    if trust:
        for account in [a.name for a in gi.discover_accounts(home)] or [".claude"]:
            _trust(home, account, {p: True for p in projects})


def _rows(report: gc.CheckReport, account: str, project: Path) -> dict:
    (acc,) = [a for a in report.accounts if a.config_dir.name == account]
    return acc.coverage[str(project.resolve())]


# ── per account ──────────────────────────────────────────────────────────────


def test_an_installed_account_reports_its_three_entries_ok(home):
    _account(home)
    report = gc.run_check(home=home)
    assert report.ok, report.problems
    (acc,) = report.accounts
    assert acc.entries == {e: "ok" for e in ALL_EVENTS}


def test_an_account_without_entries_is_a_problem_and_names_the_events(home):
    _account(home, installed=False)
    report = gc.run_check(home=home)
    assert not report.ok
    for event in ALL_EVENTS:
        assert any(event in problem and "missing" in problem for problem in report.problems)


def test_an_entry_in_another_form_is_reported_as_differing(home):
    account = _account(home)
    data = json.loads((account / "settings.json").read_text(encoding="utf-8"))
    data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 5000
    (account / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    report = gc.run_check(home=home)
    assert report.accounts[0].entries["Stop"] == "differs"
    assert not report.ok


def test_the_shim_and_the_guard_in_current_are_part_of_the_report(home):
    _account(home)
    (home / ".vnx-system" / "current" / "scripts" / "hooks" / GUARD).unlink()
    report = gc.run_check(home=home)
    assert not report.ok and any(GUARD in p for p in report.problems)
    assert "MISSING" in gc.render_report(report)


# ── decision 2: which root the registry trusts ───────────────────────────────


def test_the_trusted_root_is_the_first_vnx_dev_entry_of_the_registry(home, tmp_path):
    _account(home)
    fabric = _project(tmp_path, "fabric", project_id="vnx-dev")
    other = _project(tmp_path, "second-fabric", project_id="vnx-dev")
    (home / ".vnx").mkdir()
    (home / ".vnx" / "projects.json").write_text(json.dumps({"projects": [
        {"project_id": "vnx-dev", "path": str(fabric)},
        {"project_id": "vnx-dev", "path": str(other)}]}), encoding="utf-8")
    text = gc.render_report(gc.run_check(home=home))
    assert f"trusted repo root: {fabric.resolve()}" in text
    assert "does not exist" not in text


def test_a_first_vnx_dev_entry_that_is_not_on_disk_is_said_so(home, tmp_path):
    _account(home)
    (home / ".vnx").mkdir()
    ghost = tmp_path / "moved-away"
    (home / ".vnx" / "projects.json").write_text(
        json.dumps({"projects": [{"project_id": "vnx-dev", "path": str(ghost)}]}), encoding="utf-8")
    text = gc.render_report(gc.run_check(home=home))
    assert "does not exist on disk" in text and str(ghost) in text


def test_no_registry_means_every_project_runs_current(home):
    _account(home)
    text = gc.render_report(gc.run_check(home=home))
    assert "trusted repo root: none" in text and "~/.vnx-system/current" in text


def test_every_account_gets_its_own_trusted_root_line(home):
    _account(home)
    _account(home, ".claude-x")
    text = gc.render_report(gc.run_check(home=home))
    assert text.count("trusted repo root:") == 2


# ── per project, per event, per tool ─────────────────────────────────────────


def test_a_project_without_its_own_hooks_is_covered_by_the_user_level(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "plain")
    _register(home, project)
    report = gc.run_check(home=home)
    assert report.ok
    rows = _rows(report, ".claude", project)
    assert rows == {"UserPromptSubmit": "user", "PreToolUse:Bash": "user",
                    "PreToolUse:Skill": "user", "PreToolUse:SlashCommand": "user", "Stop": "user"}


def test_a_project_without_the_user_entry_is_missing_on_every_event(home, tmp_path):
    _account(home, installed=False)
    project = _project(tmp_path, "plain")
    _register(home, project)
    rows = _rows(gc.run_check(home=home), ".claude", project)
    assert set(rows.values()) == {"missing"}


def test_a_direct_project_registration_is_reported_with_its_form(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "direct")
    hook = _direct(project)
    _settings(project, {"UserPromptSubmit": [{"hooks": [hook]}],
                        "PreToolUse": [{"matcher": "Bash|Skill|SlashCommand", "hooks": [hook]}],
                        "Stop": [{"hooks": [hook]}]})
    _register(home, project)
    rows = _rows(gc.run_check(home=home), ".claude", project)
    assert set(rows.values()) == {"project (direct)"}


def test_a_shim_project_registration_is_reported_with_its_form(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "shimmed")
    command = hc.registered_command(GUARD, "project", str(REPO_ROOT))
    hook = {"type": "command", "command": command}
    _settings(project, {"Stop": [{"hooks": [hook]}]})
    _register(home, project)
    rows = _rows(gc.run_check(home=home), ".claude", project)
    assert rows["Stop"] == "project (shim)" and rows["UserPromptSubmit"] == "user"


def test_pretooluse_is_judged_per_tool(home, tmp_path):
    """A project entry for `Bash` alone covers Bash; Skill and SlashCommand stay with the user."""
    _account(home)
    project = _project(tmp_path, "bash-only")
    _settings(project, {"PreToolUse": [{"matcher": "Bash", "hooks": [_direct(project)]}]})
    _register(home, project)
    rows = _rows(gc.run_check(home=home), ".claude", project)
    assert rows["PreToolUse:Bash"] == "project (direct)"
    assert rows["PreToolUse:Skill"] == "user" and rows["PreToolUse:SlashCommand"] == "user"


def test_a_direct_entry_to_a_missing_path_does_not_cover(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "dead-pin")
    _settings(project, {"Stop": [{"hooks": [
        {"type": "command", "command": f"python3 {tmp_path}/gone/{GUARD}"}]}]})
    _register(home, project)
    assert _rows(gc.run_check(home=home), ".claude", project)["Stop"] == "user"


def test_unreadable_project_settings_are_unknown_and_fail_the_check(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "broken")
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    _register(home, project)
    report = gc.run_check(home=home)
    assert set(_rows(report, ".claude", project).values()) == {"unknown"}
    assert not report.ok


def test_the_same_project_differs_per_account(home, tmp_path):
    _account(home)
    _account(home, ".claude-x", installed=False)
    project = _project(tmp_path, "plain")
    _register(home, project)
    report = gc.run_check(home=home)
    assert set(_rows(report, ".claude", project).values()) == {"user"}
    assert set(_rows(report, ".claude-x", project).values()) == {"missing"}


def test_a_project_without_a_t0_folder_is_not_guarded(home, tmp_path):
    _account(home)
    client = _project(tmp_path, "clients", t0=False, project_id="vincent-clients")
    _register(home, client)
    report = gc.run_check(home=home)
    assert report.ok, "a project without a T0 folder is outside the guard and never a problem"
    text = gc.render_report(report)
    assert "not guarded (no T0)" in text
    assert report.accounts[0].coverage == {}


# ── decision 3: --require-user, every project on the user level ──────────────


def test_require_user_accepts_a_project_covered_at_user_level(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "plain")
    _register(home, project)
    report = gc.run_check(home=home, require_user=True)
    assert report.ok, report.problems


def test_require_user_fails_a_project_that_registers_the_guard_itself(home, tmp_path):
    """The closing condition of OI-2056. Without the flag a project that registers the guard in
    its own settings reads as `project (direct)` and the check exits 0; --require-user wants the
    user level to be the one that covers, so that project fails, named per event."""
    _account(home)
    project = _project(tmp_path, "self-registered")
    hook = _direct(project)
    _settings(project, {"UserPromptSubmit": [{"hooks": [hook]}],
                        "PreToolUse": [{"matcher": "Bash|Skill|SlashCommand", "hooks": [hook]}],
                        "Stop": [{"hooks": [hook]}]})
    _register(home, project)
    assert gc.run_check(home=home).ok, "without the flag the project registration covers it"
    report = gc.run_check(home=home, require_user=True)
    assert not report.ok
    for event in ALL_EVENTS:
        assert any(str(project.resolve()) in problem and event in problem
                   and "covered by the project only, not at user level" in problem
                   for problem in report.problems), report.problems


def test_require_user_leaves_a_rule_1_project_to_the_plain_check(home, tmp_path):
    """--require-user is about projects that pass rule 1; one that does not already fails the
    plain check and is not named again by this flag."""
    _account(home)
    project = _project(tmp_path, "nogit")
    project.joinpath(".git").rmdir()
    _register(home, project)
    report = gc.run_check(home=home, require_user=True)
    assert not report.ok
    assert not any("covered by the project only" in problem for problem in report.problems)


def test_the_check_cli_accepts_require_user(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "plain")
    _register(home, project)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "vnx_guard.py"), "check", "--require-user"],
        capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOT OK" not in proc.stdout


# ── an account whose own settings cannot be read ─────────────────────────────


@pytest.mark.parametrize("hooks, where", [
    ({"Stop": [{"hooks": "not a list"}]}, "hooks.Stop[0].hooks is not a list"),
    ({"Stop": [{"hooks": 17}]}, "hooks.Stop[0].hooks is not a list"),
    ({"SessionStart": "not a list"}, "hooks.SessionStart is not a list"),
    ({"Stop": [{"hooks": [{"type": "command", "command": ["x"]}]}]},
     "hooks.Stop[0].hooks[0].command is not a string"),
])
def test_an_account_with_a_malformed_hooks_tree_is_unknown_with_the_path(home, tmp_path, hooks,
                                                                         where):
    """codex on #2078: these trees reported OK, and an integer raised an uncaught TypeError."""
    account = _account(home, installed=False)
    (account / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    project = _project(tmp_path, "plain")
    _register(home, project)
    report = gc.run_check(home=home)
    (acc,) = report.accounts
    assert acc.settings_state.startswith("unknown") and where in acc.settings_state
    assert acc.entries == {event: "unknown" for event in ALL_EVENTS}
    assert set(_rows(report, ".claude", project).values()) == {"unknown"}
    assert not report.ok and any(where in problem for problem in report.problems)
    text = gc.render_report(report)
    assert "user entry Stop: unknown" in text and where in text


def test_an_account_with_invalid_json_is_unknown(home):
    account = _account(home, installed=False)
    (account / "settings.json").write_text("{not json", encoding="utf-8")
    report = gc.run_check(home=home)
    (acc,) = report.accounts
    assert acc.settings_state.startswith("unknown") and "not valid JSON" in acc.settings_state
    assert set(acc.entries.values()) == {"unknown"} and not report.ok


def test_a_malformed_account_leaves_the_other_accounts_judged(home, tmp_path):
    bad = _account(home, installed=False)
    (bad / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": 1}]}}),
                                       encoding="utf-8")
    _account(home, ".claude-x")
    project = _project(tmp_path, "plain")
    _register(home, project)
    report = gc.run_check(home=home)
    assert set(_rows(report, ".claude-x", project).values()) == {"user"}
    assert set(_rows(report, ".claude", project).values()) == {"unknown"}


# ── each account is judged with its own environment ──────────────────────────


def test_account_env_unsets_the_config_dir_for_the_default_account_only(home):
    base = {"HOME": "/elsewhere", "PATH": "/bin", "CLAUDE_CONFIG_DIR": str(home / ".claude-x")}
    default = gc.account_env(home, home / ".claude", base)
    assert "CLAUDE_CONFIG_DIR" not in default and default["HOME"] == str(home)
    other = gc.account_env(home, home / ".claude-x", {"HOME": str(home)})
    assert other["CLAUDE_CONFIG_DIR"] == str(home / ".claude-x")
    custom = gc.account_env(home, home / "custom-config", base)
    assert custom["CLAUDE_CONFIG_DIR"] == str(home / "custom-config")
    assert base["CLAUDE_CONFIG_DIR"] == str(home / ".claude-x"), "the base env is not changed"


def _two_accounts_that_disagree_on_trust(home: Path, tmp_path: Path) -> Path:
    """`~/.claude` trusts the project, `~/.claude-x` does not; the project runs the guard
    itself on every event."""
    _account(home)
    _account(home, ".claude-x")
    project = _project(tmp_path, "direct")
    hook = _direct(project)
    _settings(project, {"UserPromptSubmit": [{"hooks": [hook]}],
                        "PreToolUse": [{"matcher": "Bash|Skill|SlashCommand", "hooks": [hook]}],
                        "Stop": [{"hooks": [hook]}]})
    _register(home, project, trust=False)
    _trust(home, ".claude", {project: True})
    _trust(home, ".claude-x", {project: False})
    return project


def _session_env(home: Path) -> dict:
    """`vnx guard check` started from a session of the other account."""
    return {"HOME": str(home), "PATH": os.environ["PATH"],
            "CLAUDE_CONFIG_DIR": str(home / ".claude-x")}


def _trusted_as_claude_code_reads_it(env: dict, project: Path) -> bool:
    base = env.get("CLAUDE_CONFIG_DIR") or env["HOME"]
    data = json.loads((Path(base) / ".claude.json").read_text(encoding="utf-8"))
    record = data["projects"].get(str(project.resolve()), {})
    return record.get("hasTrustDialogAccepted") is True


def test_coverage_reads_each_accounts_own_trust_record(home, tmp_path, monkeypatch):
    """The trust record is per account: Claude Code reads `$CLAUDE_CONFIG_DIR/.claude.json` when
    the variable is set, else `$HOME/.claude.json`. Each account's rows must be computed with
    that account's environment, whatever the environment `check` itself runs in."""
    project = _two_accounts_that_disagree_on_trust(home, tmp_path)
    seen: list = []
    real = hc.coverage

    def spy(project_dir, event, tool_name=None, **kwargs):
        seen.append(dict(kwargs["env"]))
        return real(project_dir, event, tool_name, **kwargs)

    monkeypatch.setattr(hc, "coverage", spy)
    gc.run_check(home=home, env=_session_env(home))

    by_dir: dict = {}
    for env in seen:
        by_dir.setdefault(env.get("CLAUDE_CONFIG_DIR"), []).append(env)
    assert set(by_dir) == {None, str(home / ".claude-x")}, set(by_dir)
    assert len(by_dir[None]) == len(by_dir[str(home / ".claude-x")]) == 5
    assert all(env["HOME"] == str(home) for env in seen)
    assert _trusted_as_claude_code_reads_it(by_dir[None][0], project) is True
    assert _trusted_as_claude_code_reads_it(by_dir[str(home / ".claude-x")][0], project) is False


@pytest.mark.skipif(not hasattr(hc, "project_trusted"),
                    reason="the hook_coverage on this branch does not read trust yet (#2079)")
def test_with_a_trust_reading_coverage_the_two_accounts_answer_differently(home, tmp_path):
    project = _two_accounts_that_disagree_on_trust(home, tmp_path)
    report = gc.run_check(home=home, env=_session_env(home))
    assert set(_rows(report, ".claude", project).values()) == {"project (direct)"}
    assert set(_rows(report, ".claude-x", project).values()) == {"user"}


# ── inventory ────────────────────────────────────────────────────────────────


def test_the_report_says_which_scope_it_saw(home, tmp_path):
    """Without --scan-root the report covers the registry only and has to say so."""
    _account(home)
    project = _project(tmp_path / "work", "plain")
    _register(home, project)
    only_registry = gc.render_report(gc.run_check(home=home))
    assert ("scope: registry only (1 projects); "
            "pass --scan-root DIR to include unregistered projects") in only_registry
    scanned = gc.render_report(gc.run_check(home=home, scan_roots=[str(tmp_path / "work")]))
    assert "scope: registry only" not in scanned
    assert str(tmp_path / "work") in scanned


def test_scanning_finds_a_t0_folder_without_a_project_id(home, tmp_path):
    """A T0 folder without a .vnx-project-id is invisible to a `.vnx-project-id` scan, yet it is
    exactly the kind of folder the report has to surface (`not guardable`)."""
    _account(home)
    root = tmp_path / "work"
    folder = _project(root, "no-id", project_id="")
    report = gc.run_check(home=home, scan_roots=[str(root)])
    assert [project.path for project in report.projects] == [folder.resolve()]


def test_an_id_less_t0_folder_is_not_guardable_and_never_fails_the_check(home, tmp_path):
    """It is listed for the operator, with one closing line; rule 1 itself is unchanged."""
    _account(home)
    root = tmp_path / "work"
    folder = _project(root, "no-id", project_id="")
    report = gc.run_check(home=home, scan_roots=[str(root)])
    assert report.ok, report.problems
    assert report.projects[0].not_guardable == "no .vnx-project-id"
    assert _rows(report, ".claude", folder) == {"all events": "not guardable: no .vnx-project-id"}
    assert hc.t0_project_root(str(folder)) is None, "rule 1 is not relaxed"
    text = gc.render_report(report)
    assert "not guardable: no .vnx-project-id" in text
    assert "1 folder with a T0 but no usable .vnx-project-id" in text
    assert "operator decision" in text
    out = io.StringIO()
    code = gc.run_check_command(home=home, config_dirs=[], scan_roots=[str(root)], out=out,
                                require_user=True)
    assert code == 0, out.getvalue()


def test_a_scan_hit_missing_from_the_registry_is_listed_as_such(home, tmp_path):
    _account(home)
    registered = _project(tmp_path / "work", "known")
    stray = _project(tmp_path / "work" / "group", "stray")
    _register(home, registered)
    report = gc.run_check(home=home, scan_roots=[str(tmp_path / "work")])
    by_name = {p.path.name: p for p in report.projects}
    assert by_name["known"].in_registry and not by_name["stray"].in_registry
    text = gc.render_report(report)
    assert "found by scan, not in the registry" in text
    assert str(stray.resolve()) in text
    assert str(stray.resolve()) in report.accounts[0].coverage


def test_the_scan_stops_at_depth_three_and_skips_vnx_data(home, tmp_path):
    root = tmp_path / "scan"
    shallow = _project(root / "a" / "b", "ok-depth-3")
    deep = _project(root / "a" / "b" / "c", "too-deep")
    inside_data = _project(root / ".vnx-data" / "worktrees", "dispatch-x")
    names = {p.name for p in gc.scan_projects(root)}
    assert shallow.name in names
    assert deep.name not in names and inside_data.name not in names


def test_a_project_in_the_registry_and_a_scan_root_is_listed_once(home, tmp_path):
    _account(home)
    project = _project(tmp_path / "work", "once")
    _register(home, project)
    report = gc.run_check(home=home, scan_roots=[str(tmp_path / "work")])
    assert [p.path.name for p in report.projects] == ["once"]


def test_an_unreadable_registry_means_an_empty_inventory(home):
    _account(home)
    (home / ".vnx").mkdir()
    (home / ".vnx" / "projects.json").write_text("{nope", encoding="utf-8")
    assert gc.run_check(home=home).projects == []


def test_check_makes_no_write(home, tmp_path):
    account = _account(home)
    project = _project(tmp_path, "plain")
    _register(home, project)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    content = (account / "settings.json").read_bytes()
    gc.run_check(home=home, scan_roots=[str(tmp_path)])
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after and (account / "settings.json").read_bytes() == content


def test_check_exit_code_follows_the_report(home, tmp_path):
    _account(home)
    out = io.StringIO()
    assert gc.run_check_command(home=home, config_dirs=[], scan_roots=[], out=out) == 0
    assert out.getvalue().rstrip().endswith("OK")
    (home / ".vnx-system" / "current" / "scripts" / "hooks" / hc.SHIM_NAME).unlink()
    out = io.StringIO()
    assert gc.run_check_command(home=home, config_dirs=[], scan_roots=[], out=out) == 1
    assert "NOT OK" in out.getvalue()


def test_check_without_any_account_is_a_problem(home):
    report = gc.run_check(home=home)
    assert not report.ok and any("no Claude Code account" in p for p in report.problems)


# ── vnx doctor ───────────────────────────────────────────────────────────────


def test_doctor_passes_on_a_machine_without_a_claude_account(home, monkeypatch):
    from vnx_cli.commands import doctor

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    check = doctor._check_user_guard()
    assert check.name == "guard:user-level" and check.status == doctor.PASS


def test_doctor_warns_when_an_account_lacks_the_guard_and_passes_once_installed(home, monkeypatch):
    from vnx_cli.commands import doctor

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    account = _account(home, installed=False)
    warned = doctor._check_user_guard()
    assert warned.status == doctor.WARN and "vnx guard install" in warned.detail
    assert gi.write_settings(account / "settings.json").status == "written"
    assert doctor._check_user_guard().status == doctor.PASS


def test_doctor_runs_the_guard_check_in_its_full_run(home, monkeypatch, tmp_path, capsys):
    import argparse

    from vnx_cli.commands import doctor

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    (project / ".vnx").mkdir(parents=True)
    doctor.vnx_doctor(argparse.Namespace(project_dir=str(project), json=True, strict=False))
    names = [c["name"] for c in json.loads(capsys.readouterr().out)["checks"]]
    assert "guard:user-level" in names


# ── the shim's rule 1, a second own entry, the missing-guard notice ──────────


def test_a_project_with_a_t0_folder_but_no_project_id_is_not_guardable(home, tmp_path):
    """Decision 3: a T0 folder without a usable .vnx-project-id is listed as not guardable and
    never fails the plain check. Rule 1 itself still turns the shim away from it."""
    _account(home)
    project = _project(tmp_path, "noid", project_id="")
    _register(home, project)
    report = gc.run_check(home=home)
    assert report.ok, report.problems
    rows = _rows(report, ".claude", project)
    assert rows == {"all events": "not guardable: no .vnx-project-id"}
    assert hc.t0_project_root(str(project)) is None, "rule 1 is not relaxed"
    assert "not guardable: no .vnx-project-id" in gc.render_report(report)


def test_a_project_without_a_git_entry_is_not_guarded_with_the_reason(home, tmp_path):
    _account(home)
    project = _project(tmp_path, "nogit")
    project.joinpath(".git").rmdir()
    _register(home, project)
    report = gc.run_check(home=home)
    assert not report.ok
    assert ".git" in _rows(report, ".claude", project)["all events"]


def test_a_project_under_dispatch_worktrees_is_not_guarded(home, tmp_path):
    _account(home)
    project = _project(tmp_path / ".vnx-data" / "worktrees", "wt")
    _register(home, project)
    report = gc.run_check(home=home)
    assert not report.ok and "not guarded" in _rows(report, ".claude", project)["all events"]


def test_check_reports_a_second_own_entry_as_not_ok_by_event_name(home):
    account = _account(home)
    path = account / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    older = dict(gi.guard_hook(), timeout=5000)
    settings["hooks"]["Stop"].append({"matcher": "", "hooks": [older]})
    path.write_text(json.dumps(settings), encoding="utf-8")
    report = gc.run_check(home=home)
    assert not report.ok
    assert report.accounts[0].entries["Stop"] == gi.STATE_DUPLICATE
    assert report.accounts[0].entries["UserPromptSubmit"] == gi.STATE_OK
    assert any("Stop has more than one entry" in p for p in report.problems)


def test_doctor_names_the_install_when_no_account_has_the_guard(home, monkeypatch):
    from vnx_cli.commands import doctor

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _account(home, installed=False)
    check = doctor._check_user_guard()
    assert check.status == doctor.WARN and "vnx guard install" in check.detail


def test_the_notice_is_one_line_only_while_no_account_has_the_guard(home):
    assert gi.missing_guard_notice(home) is None, "no account: nothing to install into"
    account = _account(home, installed=False)
    notice = gi.missing_guard_notice(home)
    assert notice and "\n" not in notice and "vnx guard install" in notice
    assert gi.write_settings(account / "settings.json").status == "written"
    assert gi.missing_guard_notice(home) is None
    (account / "settings.json").write_text("{not json", encoding="utf-8")
    assert gi.missing_guard_notice(home) is not None, "an unreadable file is not an install"
