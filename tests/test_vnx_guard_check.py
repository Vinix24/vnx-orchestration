"""tests/test_vnx_guard_check.py — `vnx guard check` and the `vnx doctor` check built on it.

Plan t0-guard-gebruikersniveau, § "User-level registration", deliverable G2. A tmp ``HOME`` holds
the accounts, the registry ``~/.vnx/projects.json`` and ``~/.vnx-system/current``; the projects
are tmp directories whose settings follow the documented rules of ``hook_coverage`` (a direct
entry needs an existing path, a PreToolUse matcher must match the tool, an unreadable file is
``unknown``). Nothing here touches the real ``~/.claude``, ``~/.claude-*`` or ``~/.vnx``.
"""
from __future__ import annotations

import io
import json
import shutil
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


def _register(home: Path, *projects: Path) -> None:
    (home / ".vnx").mkdir(exist_ok=True)
    entries = [{"project_id": p.name, "path": str(p)} for p in projects]
    (home / ".vnx" / "projects.json").write_text(json.dumps({"projects": entries}), encoding="utf-8")


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


# ── inventory ────────────────────────────────────────────────────────────────


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
    assert warned.status == doctor.WARN and "vnx guard check" in warned.detail
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
