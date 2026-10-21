"""`vnx providers` on a tmp HOME: verbs, exit codes, consequence lines and both entrances.

Every test runs with HOME and VNX_PROVIDER_SWITCH_FILE inside tmp_path and a tmp project
registry: the operator's real ~/.vnx (switch file, audit log, lock, quarantine, projects.json)
and ~/Library/LaunchAgents are never read or written. The tmp HOME also carries no ambient
VNX_OVERRIDE_* variable: a test that needs one sets it itself.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import provider_switch as ps  # noqa: E402
import providers_cli  # noqa: E402

CLI = REPO_ROOT / "scripts" / "providers_cli.py"
PROJECT = "demo-app"
CONFIG_KEYS = ("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "VNX_TAGGER_PROVIDER", "VNX_SCOUT_PROVIDER",
               "VNX_TAGGER_ENABLED", "VNX_SCOUT_PREPASS")


def strip_ambient_overrides(monkeypatch) -> None:
    """Remove every ambient VNX_OVERRIDE_* variable, not only the provider brakes.

    ``config_registry.get`` returns ``VNX_OVERRIDE_<key>`` before anything else, so an override
    left in the shell (even an empty one) changes what the consequence lines read.
    """
    for key in [k for k in os.environ if k.startswith("VNX_OVERRIDE_")]:
        monkeypatch.delenv(key)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A tmp HOME holding the switch file, its audit log and the project registry."""
    home = tmp_path / "home"
    (home / ".vnx").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(home / ".vnx" / "providers.json"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    strip_ambient_overrides(monkeypatch)
    outside = tmp_path / "cwd"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return home


@pytest.fixture
def no_markers(monkeypatch):
    for marker in ps.DISPATCH_MARKER_ENV:
        monkeypatch.delenv(marker, raising=False)


@pytest.fixture
def live(home, no_markers):
    """The live write path: no dispatch marker and a tmp working directory outside any repo."""
    return home


def switch_path(home: Path) -> Path:
    return home / ".vnx" / "providers.json"


def write_switches(home: Path, document) -> None:
    text = document if isinstance(document, str) else json.dumps(document)
    switch_path(home).write_text(text, encoding="utf-8")


def off(reason: str = "r") -> dict:
    return {"enabled": False, "changed_at": "2026-10-12T08:00:00Z", "changed_by": "op", "reason": reason}


def on(reason: str = "r") -> dict:
    return {**off(reason), "enabled": True}


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """The CLI in its own process, on the tmp HOME the fixtures pinned in os.environ."""
    env = {k: v for k, v in os.environ.items() if k not in ps.DISPATCH_MARKER_ENV}
    return subprocess.run([sys.executable, str(CLI), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=120)


def run_main(capsys, *args: str):
    code = providers_cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_text_on_a_tmp_home(home, tmp_path):
    result = run_cli("list", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "(missing: no switches, everything on)" in out
    assert f"Current project: {PROJECT}" in out
    for family in ps.SWITCHABLE_FAMILIES:
        assert f"  {family:<11} effective on  (default)" in out
    assert "Never switchable:" in out and "operator decision 2b" in out
    assert (f"Enforcement: live, all {len(ps.LAUNCH_FUNCTIONS)} launch functions call "
            "provider_switch.require") in out
    assert "NOT enforced yet" not in out and "pending B" not in out
    for entry in ps.ENFORCED:
        assert entry not in out
    assert "Projects (~/.vnx/projects.json):" in out and "no project registry" in out
    assert "Overrides (VNX_OVERRIDE_PROVIDER_*):\n  none" in out
    assert "Consequences: none" in out


def test_list_json(home, tmp_path):
    write_switches(home, {"version": 1, "global": {"glm": off("proxy weg")},
                          "projects": {PROJECT: {"glm": on("hier wel")}}})
    result = run_cli("list", "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["readable"] and report["exists"] and report["version"] == 1 and report["project"] == PROJECT
    rows = {row["family"]: row for row in report["families"]}
    assert list(rows) == list(ps.SWITCHABLE_FAMILIES)
    glm = rows["glm"]
    assert glm["global"]["enabled"] is False and glm["global"]["reason"] == "proxy weg"
    assert glm["projects"] == {PROJECT: {"enabled": True, "changed_at": "2026-10-12T08:00:00Z",
                                         "changed_by": "op", "reason": "hier wel"}}
    assert glm["effective"]["allowed"] is True and glm["effective"]["source"] == "project"
    assert report["enforcement"] == {"launch_functions": len(ps.LAUNCH_FUNCTIONS)}
    assert report["enforcement"]["launch_functions"] == len(ps.LAUNCH_FUNCTIONS)
    assert set(report["non_switchable"]) == {"claude", "codex", "tooling", "unresolved"}
    assert report["projects"]["error"] == "no project registry"
    assert report["overrides"] == {"environment": {}, "launchd": {}}
    assert report["consequences"] == []


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_bare_command_and_flags_mean_list(args, home, capsys):
    code, out, _ = run_main(capsys, *args)
    assert code == 0
    if args:
        assert json.loads(out)["project"] == PROJECT
    else:
        assert out.startswith("Provider switch:")


def test_list_reports_an_unreadable_file_and_exits_3(home, tmp_path):
    write_switches(home, '{"version": 1, "global": ')
    result = run_cli("list", cwd=tmp_path)
    assert result.returncode == providers_cli.EXIT_UNREADABLE
    assert "Provider switch: UNREADABLE" in result.stdout and "invalid JSON" in result.stdout
    assert "vnx providers repair" in result.stdout
    assert "effective OFF (unreadable" in result.stdout


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def test_check_allowed_and_refused(home, tmp_path):
    write_switches(home, {"version": 1, "global": {"deepseek": off("tegoed sparen")},
                          "projects": {"other-app": {"deepseek": on()}}})
    refused = run_cli("check", "deepseek-harness", "--consumer", "nightly.sh", cwd=tmp_path)
    assert refused.returncode == ps.EXIT_PROVIDER_SWITCHED_OFF == 4
    assert refused.stdout == ""
    assert "provider_switched_off: deepseek-harness family=deepseek source=global" in refused.stderr
    assert "consumer=nightly.sh" in refused.stderr and "tegoed sparen" in refused.stderr

    allowed = run_cli("check", "kimi-k3", cwd=tmp_path)
    assert allowed.returncode == 0 and allowed.stdout.startswith("allowed: kimi-k3 family=kimi")
    assert run_cli("check", "claude", cwd=tmp_path).returncode == 0
    assert run_cli("check", "deepseek", "--project", "other-app", cwd=tmp_path).returncode == 0

    unknown = run_cli("check", "mistral-large", cwd=tmp_path)
    assert unknown.returncode == 4 and "source=unknown-identifier" in unknown.stderr

    write_switches(home, "{broken")
    unreadable = run_cli("check", "glm", cwd=tmp_path)
    assert unreadable.returncode == 4 and "source=unreadable" in unreadable.stderr
    assert run_cli("check", "codex", cwd=tmp_path).returncode == 0


# ---------------------------------------------------------------------------
# Write verbs: live, usage errors
# ---------------------------------------------------------------------------

def test_write_verbs_are_live_in_their_own_process(home, tmp_path):
    """Every launch function calls require, so `off` from outside any worktree and without a
    dispatch marker writes the file and its audit line; `check` then refuses, `on` restores."""
    result = run_cli("off", "deepseek", "--reason", "tegoed sparen", cwd=tmp_path)
    assert result.returncode == providers_cli.EXIT_OK == 0, result.stderr
    assert "not enforced yet" not in result.stderr
    assert ps.load_state(switch_path(home)).global_switches["deepseek"].enabled is False
    assert [r["op"] for r in audit_records(home)] == ["set"]
    assert run_cli("check", "deepseek-harness", cwd=tmp_path).returncode == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert run_cli("on", "deepseek", "--reason", "weer aan", cwd=tmp_path).returncode == 0
    assert run_cli("check", "deepseek-harness", cwd=tmp_path).returncode == 0


@pytest.mark.parametrize("args", [
    ["off", "deepseek"], ["on", "glm", "--reason", "x"], ["off", "kimi", "--project", PROJECT],
    ["reset", "gemini", "--project", PROJECT], ["repair"], ["repair", "--empty"],
])
def test_write_verbs_refuse_with_a_dispatch_marker(args, live, capsys, monkeypatch):
    """The only things that refuse a write are the caller's worktree and a dispatch marker:
    with one in the environment every write verb exits 5, names it and writes nothing."""
    monkeypatch.setenv("VNX_WORKER_DISPATCH_ID", "dlv-x")
    code, _, err = run_main(capsys, *args)
    assert code == providers_cli.EXIT_WRITE_REFUSED == 5
    assert "write refused" in err and "VNX_WORKER_DISPATCH_ID" in err
    assert "not enforced yet" not in err and "pending" not in err
    assert not switch_path(live).exists() and not ps.audit_log_path(switch_path(live)).exists()


@pytest.mark.parametrize("args,needle", [
    (["off", "claude"], "never switchable (operator decision 2b)"),
    (["on", "codex"], "never switchable"),
    (["off", "tooling"], "gh-based gates"),
    (["off", "unresolved"], "auto is resolved by the router"),
    (["off", "mistral"], "unknown family 'mistral'"),
    (["off", "deepseek", "--project", "Bad_Id"], "invalid project id"),
    (["reset", "deepseek"], "--project"),
    (["frobnicate"], "invalid choice"),
])
def test_usage_errors_exit_2(args, needle, home, tmp_path):
    result = run_cli(*args, cwd=tmp_path)
    assert result.returncode == providers_cli.EXIT_USAGE == 2
    assert needle in result.stderr


# ---------------------------------------------------------------------------
# The live write path (pending emptied in this process) and its consequence lines
# ---------------------------------------------------------------------------

def test_off_on_reset_and_consequences(live, capsys):
    code, out, _ = run_main(capsys, "off", "deepseek", "--reason", "tegoed sparen")
    assert code == 0
    assert "deepseek switched OFF globally (was unset)" in out
    assert "the plan-gate tiebreaker provider deepseek_harness is switched off: the tiebreaker will refuse" in out
    assert "the tagger provider deepseek is switched off: tagger calls will be skipped" in out
    assert "the scout provider deepseek is switched off: scout calls will be skipped" in out
    assert "no second review seat" not in out
    line = json.loads(ps.audit_log_path(switch_path(live)).read_text(encoding="utf-8").splitlines()[0])
    assert (line["op"], line["family"], line["scope"], line["reason"]) == ("set", "deepseek", "global", "tegoed sparen")

    code, out, _ = run_main(capsys, "on", "deepseek", "--project", PROJECT)
    assert code == 0 and f"deepseek switched on for project {PROJECT}" in out
    code, out, _ = run_main(capsys, "list", "--json", "--project", PROJECT)
    effective = {r["family"]: r["effective"] for r in json.loads(out)["families"]}
    assert (effective["deepseek"]["allowed"], effective["deepseek"]["source"]) == (True, "project")
    code, out, _ = run_main(capsys, "list", "--json", "--project", "other-app")
    effective = {r["family"]: r["effective"] for r in json.loads(out)["families"]}
    assert (effective["deepseek"]["allowed"], effective["deepseek"]["source"]) == (False, "global")

    code, out, _ = run_main(capsys, "reset", "deepseek", "--project", PROJECT, "--reason", "terug")
    assert code == 0 and f"deepseek override removed for project {PROJECT}" in out
    code, out, _ = run_main(capsys, "reset", "deepseek", "--project", PROJECT)
    assert code == 0 and "nothing changed" in out
    assert [json.loads(x)["op"] for x in ps.audit_log_path(switch_path(live)).read_text().splitlines()] == [
        "set", "set", "clear"]


def test_consequence_no_second_review_seat(live, capsys, monkeypatch):
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    assert run_main(capsys, "off", "kimi")[0] == 0
    code, out, _ = run_main(capsys, "off", "deepseek")
    assert code == 0
    assert ("no second review seat: every gate after codex_gate in the takeover chain "
            "(kimi_gate, deepseek_gate) is switched off, so a PR gets codex alone") in out
    code, out, _ = run_main(capsys, "list")
    assert "no second review seat" in out


def test_consequence_no_second_review_seat_with_glm_in_an_explicit_chain(live, capsys, monkeypatch):
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate,glm_gate")
    for family in ("kimi", "glm"):
        assert run_main(capsys, "off", family)[0] == 0
    code, out, _ = run_main(capsys, "off", "deepseek")
    assert code == 0
    assert ("no second review seat: every gate after codex_gate in the takeover chain "
            "(kimi_gate, deepseek_gate, glm_gate) is switched off, so a PR gets codex alone") in out


def test_consequence_plan_gate_cannot_certify(live, capsys, monkeypatch):
    import plan_gate_panel
    monkeypatch.setattr(plan_gate_panel, "load_panel_seats", lambda: [
        {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k3"},
        {"label": "glm", "provider": "glm-harness", "model_arg": "glm-5.2"},
        {"label": "codex", "provider": "codex", "model_arg": "gpt-5.5"},
    ])
    run_main(capsys, "off", "kimi")
    code, out, _ = run_main(capsys, "off", "glm")
    assert code == 0
    assert "only 1 of 3 plan-gate seat(s) enabled (codex): a plan-gate cannot certify" in out


def test_write_on_an_unreadable_file_exits_3(live, capsys):
    write_switches(live, "{broken")
    code, _, err = run_main(capsys, "off", "glm")
    assert code == providers_cli.EXIT_UNREADABLE and "vnx providers repair" in err
    assert switch_path(live).read_text() == "{broken"


def test_repair_verbs(live, capsys):
    run_main(capsys, "off", "gemini", "--reason", "geen cli")
    run_main(capsys, "off", "local", "--project", PROJECT)
    write_switches(live, '{"version": 1, "global": {"gemini": {"enabled": "nee"}}}')
    assert run_main(capsys, "list")[0] == providers_cli.EXIT_UNREADABLE

    code, out, _ = run_main(capsys, "repair")
    assert code == 0
    assert "the unreadable file is kept as providers.json.corrupt-" in out
    assert '"gemini"' in out and '"local"' in out
    corrupt = [p for p in switch_path(live).parent.iterdir() if ".corrupt-" in p.name]
    assert len(corrupt) == 1 and '"nee"' in corrupt[0].read_text()
    code, out, _ = run_main(capsys, "list", "--json")
    effective = {r["family"]: r["effective"]["allowed"] for r in json.loads(out)["families"]}
    assert effective["gemini"] is False and effective["local"] is False and effective["kimi"] is True

    code, _, err = run_main(capsys, "repair")
    assert code == providers_cli.EXIT_WRITE_FAILED and "repair only handles an unreadable file" in err

    write_switches(live, "{broken")
    ps.audit_log_path(switch_path(live)).unlink()
    code, _, err = run_main(capsys, "repair")
    assert code == providers_cli.EXIT_UNREADABLE and "--empty" in err
    code, out, _ = run_main(capsys, "repair", "--empty")
    assert code == 0 and "an empty version 1 file" in out
    assert json.loads(switch_path(live).read_text()) == {"version": 1, "global": {}, "projects": {}}


# ---------------------------------------------------------------------------
# A replace that fails after the audit line: an abort record, never a phantom replay
# ---------------------------------------------------------------------------

def audit_records(home: Path) -> List[dict]:
    text = ps.audit_log_path(switch_path(home)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _no_space(*_args, **_kwargs):
    raise OSError(28, "No space left on device")


# writer -> (verbs that build the state, the verb whose replace fails, the op of its audit line)
FAILED_WRITES = {
    "set": ([["off", "deepseek", "--reason", "sparen"]], ["off", "glm", "--reason", "proxy weg"], "set"),
    "clear": ([["off", "kimi"], ["on", "kimi", "--project", PROJECT]], ["reset", "kimi", "--project", PROJECT],
              "clear"),
    "repair": ([["off", "deepseek", "--reason", "sparen"]], ["repair", "--empty"], "repair"),
}


@pytest.mark.parametrize("writer", sorted(FAILED_WRITES))
def test_a_failed_replace_is_aborted_and_never_replayed(writer, live, capsys, monkeypatch):
    setup, failing, op = FAILED_WRITES[writer]
    for args in setup:
        assert run_main(capsys, *args)[0] == 0
    expected = json.loads(switch_path(live).read_text(encoding="utf-8"))
    if writer == "repair":
        write_switches(live, "{broken")
    before = switch_path(live).read_bytes()

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", _no_space)
        code, _, err = run_main(capsys, *failing)
    log_after_failure = audit_records(live)
    assert code == providers_cli.EXIT_WRITE_FAILED == 1
    assert switch_path(live).read_bytes() == before

    # repair rebuilds the state the setup left: the change that never landed is not replayed
    write_switches(live, "{truncated by a crash")
    repaired, out, _ = run_main(capsys, "repair")
    assert repaired == 0, out
    assert json.loads(switch_path(live).read_text(encoding="utf-8")) == expected

    original, abort = log_after_failure[-2], log_after_failure[-1]
    assert original["op"] == op and original.get("id")
    assert (abort["op"], abort["ref"]) == ("abort", original["id"])
    assert abort["id"] != original["id"] and "No space left on device" in abort["error"]
    what = "repair" if writer == "repair" else "write"
    assert f"vnx providers: {what} failed: [Errno 28] No space left on device" in err
    assert f"the audit line with id {original['id']} is followed by an abort record" in err


def test_an_abort_that_cannot_be_appended_names_the_unconfirmed_line(live, capsys, monkeypatch):
    real_append = ps._append_audit_record

    def append_all_but_the_abort(path, record):
        if record["op"] == "abort":
            raise OSError(5, "Input/output error")
        real_append(path, record)

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", _no_space)
        patched.setattr(ps, "_append_audit_record", append_all_but_the_abort)
        code, _, err = run_main(capsys, "off", "gemini")
    records = audit_records(live)
    assert code == providers_cli.EXIT_WRITE_FAILED and not switch_path(live).exists()
    assert [r["op"] for r in records] == ["set"]
    assert "vnx providers: write failed: [Errno 28] No space left on device" in err
    assert f"the audit log may hold one unconfirmed line: id {records[0]['id']}" in err
    assert "Input/output error" in err


def _git(*args, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


def test_write_refused_from_a_linked_worktree(live, capsys, tmp_path, monkeypatch):
    main = tmp_path / "tmp-repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    linked = tmp_path / "tmp-repo-wt"
    _git("worktree", "add", "-q", str(linked), cwd=main)
    assert not str(CLI.resolve()).startswith(str(linked.resolve()))

    monkeypatch.chdir(linked)
    code, _, err = run_main(capsys, "off", "deepseek")
    assert code == 5 and "linked git worktree" in err and str(linked.resolve()) in err
    assert not switch_path(live).exists()

    monkeypatch.chdir(main)
    code, out, _ = run_main(capsys, "off", "deepseek")
    assert code == 0 and "deepseek switched OFF globally" in out


@pytest.mark.parametrize("marker", ps.DISPATCH_MARKER_ENV)
@pytest.mark.parametrize("args", [["off", "glm"], ["reset", "glm", "--project", PROJECT], ["repair", "--empty"]])
def test_write_refused_with_a_dispatch_marker(marker, args, live, capsys, monkeypatch):
    monkeypatch.setenv(marker, "20261012-x")
    code, _, err = run_main(capsys, *args)
    assert code == 5 and marker in err
    assert not switch_path(live).exists()


# ---------------------------------------------------------------------------
# list: overrides, projects and engines
# ---------------------------------------------------------------------------

def test_list_shows_overrides_from_env_and_launchd(home, capsys, monkeypatch):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    with (agents / "com.vnx.analyzer-nightly.plist").open("wb") as fh:
        plistlib.dump({"Label": "com.vnx.analyzer-nightly", "EnvironmentVariables": {
            "VNX_OVERRIDE_PROVIDER_DEEPSEEK": "off", "VNX_OVERRIDE_PROVIDER_GLM": "yes", "PATH": "/usr/bin",
        }}, fh)
    with (agents / "com.example.other.plist").open("wb") as fh:
        plistlib.dump({"EnvironmentVariables": {"VNX_OVERRIDE_PROVIDER_KIMI": "off"}}, fh)
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_KIMI", "0")
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_GEMINI", "of")

    code, out, _ = run_main(capsys, "list", "--json")
    overrides = json.loads(out)["overrides"]
    assert overrides["environment"]["VNX_OVERRIDE_PROVIDER_KIMI"]["meaning"] == "switches kimi off for that process"
    assert overrides["environment"]["VNX_OVERRIDE_PROVIDER_GEMINI"]["meaning"].startswith("invalid token")
    assert list(overrides["launchd"]) == ["com.vnx.analyzer-nightly.plist"]
    nightly = overrides["launchd"]["com.vnx.analyzer-nightly.plist"]
    assert nightly["VNX_OVERRIDE_PROVIDER_DEEPSEEK"]["meaning"] == "switches deepseek off for that process"
    assert nightly["VNX_OVERRIDE_PROVIDER_GLM"]["meaning"].startswith("ignored")
    assert "PATH" not in nightly

    code, out, _ = run_main(capsys, "list")
    assert "environment: VNX_OVERRIDE_PROVIDER_KIMI=0 (switches kimi off for that process)" in out
    assert ("launchd com.vnx.analyzer-nightly.plist: VNX_OVERRIDE_PROVIDER_DEEPSEEK=off "
            "(switches deepseek off for that process)") in out
    assert "kimi        effective OFF (override" in out


def test_list_reports_a_malformed_plist_and_keeps_the_valid_one(home, tmp_path):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.vnx.broken.plist").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>')
    with (agents / "com.vnx.valid.plist").open("wb") as fh:
        plistlib.dump({"Label": "com.vnx.valid",
                       "EnvironmentVariables": {"VNX_OVERRIDE_PROVIDER_GLM": "off"}}, fh)

    listed = run_cli("list", cwd=tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert "launchd com.vnx.broken.plist: unreadable: " in listed.stdout
    assert ("launchd com.vnx.valid.plist: VNX_OVERRIDE_PROVIDER_GLM=off "
            "(switches glm off for that process)") in listed.stdout

    as_json = run_cli("list", "--json", cwd=tmp_path)
    assert as_json.returncode == 0, as_json.stderr
    launchd = json.loads(as_json.stdout)["overrides"]["launchd"]
    assert launchd["com.vnx.broken.plist"]["error"].startswith("unreadable: ")
    assert launchd["com.vnx.valid.plist"]["VNX_OVERRIDE_PROVIDER_GLM"]["meaning"] == (
        "switches glm off for that process")


def _engine(root: Path, families: List[str] = None) -> Path:
    (root / "scripts" / "lib").mkdir(parents=True)
    if families is not None:
        (root / "scripts" / "lib" / "provider_switch.py").write_text(
            f"SWITCHABLE_FAMILIES = {tuple(families)!r}\n", encoding="utf-8")
    return root


def _linked_project(root: Path, engine: Path) -> Path:
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "vnx-system").symlink_to(engine)
    return root


def test_list_shows_per_project_engine_knowledge(home, capsys, tmp_path):
    fabric = _engine(tmp_path / "fabric", list(ps.SWITCHABLE_FAMILIES))
    (fabric / "scripts" / "lib" / "vnx_paths.py").write_text("", encoding="utf-8")
    old_engine = _engine(tmp_path / "versions" / "v1.7.1")
    partial_engine = _engine(tmp_path / "versions" / "v9.0.0", ["deepseek", "glm", "kimi"])
    projects = [
        {"project_id": "fabric-proj", "path": str(fabric)},
        {"project_id": "old-proj", "path": str(_linked_project(tmp_path / "old", old_engine))},
        {"project_id": "partial-proj", "path": str(_linked_project(tmp_path / "partial", partial_engine))},
        {"project_id": "plain-proj", "path": str(tmp_path / "plain")},
    ]
    (tmp_path / "plain").mkdir()
    (home / ".vnx" / "projects.json").write_text(json.dumps({"schema_version": 2, "projects": projects}))
    write_switches(home, {"version": 1, "global": {"gemini": off()},
                          "projects": {"partial-proj": {"deepseek": off()}, "fabric-proj": {"gemini": on()}}})

    code, out, _ = run_main(capsys, "list", "--json")
    rows = {row["project_id"]: row for row in json.loads(out)["projects"]["projects"]}
    assert rows["fabric-proj"]["knows_switch"] and rows["fabric-proj"]["engine_source"] == "fabric checkout"
    assert rows["fabric-proj"]["off_families"] == []
    assert not rows["old-proj"]["knows_switch"] and rows["old-proj"]["engine_source"] == "linked engine"
    assert rows["partial-proj"]["knows_switch"]
    assert rows["partial-proj"]["off_families"] == ["deepseek", "gemini"]
    assert rows["partial-proj"]["off_families_unknown_to_engine"] == ["gemini"]
    assert not rows["plain-proj"]["knows_switch"] and rows["plain-proj"]["engine_root"] is None
    assert all("not covered" in row["application_code"] for row in rows.values())

    code, out, _ = run_main(capsys, "list")
    assert "fabric-proj        engine knows the switch (fabric checkout" in out
    assert "old-proj           engine does NOT know the switch" in out and "every provider stays on there" in out
    assert "WARNING: gemini is off but this engine does not know the family: it keeps calling it" in out
    assert out.count("application code: not covered") == 4
    assert out.count("WARNING:") == 1


def test_engine_families_parses_without_importing(tmp_path):
    engine = _engine(tmp_path / "e", ["deepseek", "glm"])
    (engine / "scripts" / "lib" / "provider_switch.py").write_text(
        "import sys\nsys.exit('must not run')\nSWITCHABLE_FAMILIES = ('deepseek', 'glm')\n", encoding="utf-8")
    assert providers_cli.engine_families(engine) == ("deepseek", "glm")
    assert providers_cli.engine_families(tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# Entrances, mode tier, worker deny patterns
# ---------------------------------------------------------------------------

def _pip(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ps.DISPATCH_MARKER_ENV}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run([sys.executable, "-m", "vnx_cli.main", "providers", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=120)


def test_pip_entrance_runs_the_same_module(home, tmp_path):
    listed = _pip("list", "--json", cwd=tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["project"] == PROJECT
    assert json.loads(_pip("--json", cwd=tmp_path).stdout)["enforcement"]["launch_functions"] == len(ps.LAUNCH_FUNCTIONS)
    assert _pip("off", "claude", cwd=tmp_path).returncode == 2
    written = _pip("off", "deepseek", "--reason", "x y", cwd=tmp_path)
    assert written.returncode == 0, written.stderr
    assert _pip("check", "deepseek", cwd=tmp_path).returncode == 4
    assert _pip("check", "mistral", cwd=tmp_path).returncode == 4
    assert _pip("check", "kimi", cwd=tmp_path).returncode == 0


PASS_THROUGH = [
    ["--help"],
    ["-h"],
    ["list", "--json"],
    ["check", "deepseek-harness", "--project", "other-app"],
    ["--no-such-flag", "x"],
    ["list", "--no-such-flag"],
]


@pytest.mark.parametrize("args", PASS_THROUGH)
def test_pip_entrance_passes_every_argument_through_verbatim(args, home, monkeypatch):
    """The argv the providers_cli.py subprocess receives is exactly what followed `vnx providers`."""
    import vnx_cli._reexec
    import vnx_cli.main
    from vnx_cli.commands import providers as pip_providers
    received = []

    def record(argv, **kwargs):
        received.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(vnx_cli._reexec, "maybe_reexec_pinned", lambda argv=None: None)
    monkeypatch.setattr(pip_providers.subprocess, "run", record)
    monkeypatch.setattr(sys, "argv", ["vnx", "providers", *args])
    with pytest.raises(SystemExit) as exc:
        vnx_cli.main.main()
    assert exc.value.code == 0
    assert pip_providers.resolve_providers_script() == CLI
    assert received == [[sys.executable, str(CLI), *args]]


@pytest.mark.parametrize("error", [
    OSError("no such interpreter"),
    subprocess.SubprocessError("launch failed"),
])
def test_pip_entrance_returns_failure_code_when_the_launch_raises(error, home, monkeypatch, capsys):
    """A launch that raises is a defined failure: one stderr line and exit 1, never a traceback."""
    from vnx_cli.commands import providers as pip_providers

    def boom(argv, **kwargs):
        raise error

    monkeypatch.setattr(pip_providers.subprocess, "run", boom)
    args = type("Args", (), {"providers_args": ["list"]})()
    assert pip_providers.vnx_providers(args) == 1
    err = capsys.readouterr().err
    assert err.strip() == f"vnx providers: could not start providers_cli.py: {error}"


def test_pip_entrance_does_not_swallow_other_launch_errors(home, monkeypatch):
    """Only OSError and SubprocessError are mapped; anything wider still surfaces."""
    from vnx_cli.commands import providers as pip_providers

    def boom(argv, **kwargs):
        raise ValueError("not a launch failure")

    monkeypatch.setattr(pip_providers.subprocess, "run", boom)
    with pytest.raises(ValueError):
        pip_providers.vnx_providers(type("Args", (), {"providers_args": []})())


def test_pip_entrance_help_is_the_script_help(home, tmp_path):
    for flag in ("--help", "-h"):
        result = _pip(flag, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "Turn model providers on and off." in result.stdout
    checked = _pip("check", "deepseek-harness", "--project", "other-app", cwd=tmp_path)
    assert checked.returncode == 0 and "scope=other-app" in checked.stdout


def test_bin_vnx_entrance(home, no_markers, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ps.DISPATCH_MARKER_ENV}
    vnx = str(REPO_ROOT / "bin" / "vnx")
    allowed = subprocess.run([vnx, "providers", "check", "claude"], cwd=str(tmp_path), env=env,
                             capture_output=True, text=True, timeout=120)
    assert allowed.returncode == 0, allowed.stderr
    written = subprocess.run([vnx, "providers", "off", "deepseek"], cwd=str(tmp_path), env=env,
                             capture_output=True, text=True, timeout=120)
    assert written.returncode == 0, written.stderr
    assert ps.load_state(switch_path(home)).global_switches["deepseek"].enabled is False


def test_providers_is_in_the_starter_operator_tier():
    import vnx_mode
    assert "providers" in vnx_mode.TIER_STARTER_OPERATOR
    for mode in (vnx_mode.VNXMode.STARTER, vnx_mode.VNXMode.OPERATOR):
        vnx_mode.check_command_allowed("providers", mode=mode)


DENIED = [
    "vnx providers off deepseek",
    "vnx providers on glm --project demo-app",
    "./bin/vnx providers reset kimi --project demo-app",
    "/opt/vnx/bin/vnx providers repair --empty",
    "python3 scripts/providers_cli.py list",
    "python3 /abs/scripts/providers_cli.py check deepseek",
]
DENIED += [
    "python3 scripts/providers_cli.py off kimi",
    "bin/vnx providers off kimi",
    "vnx providers repair --empty",
]
READ_ONLY = [
    "vnx providers",
    "vnx providers list --json",
    "vnx providers check deepseek",
    "python3 -m pytest tests/test_providers_cli.py -q",
    "pytest tests/test_providers_cli.py::test_x",
    "cat scripts/lib/provider_switch.py",
]


def test_template_denies_the_write_verbs_for_every_profile():
    from worker_permissions import PermissionProfile, match_bash_deny
    template = yaml.safe_load((REPO_ROOT / "templates" / "worker_permissions.yaml.tmpl").read_text(encoding="utf-8"))
    for role, raw in template["profiles"].items():
        profile = PermissionProfile(role=role, bash_deny_patterns=raw["bash_deny_patterns"])
        for command in DENIED:
            assert match_bash_deny(command, profile), (role, command)
        for command in READ_ONLY:
            assert match_bash_deny(command, profile) is None, (role, command)
