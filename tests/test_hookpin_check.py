#!/usr/bin/env python3
"""Tests for scripts/lib/hookpin_check.py and vnx_doctor.py::check_hook_pins() (OI-1123).

Covers the case list the round-2 security audit specified plus the two
defects found while wiring the SessionStart surface into settings.json:

  1. dead / live absolute pin
  2. no .claude directory / no hooks key (absent-by-design, not a defect)
  3. a disabled matcher (_DISABLED_MATCHER_MARKERS)
  4. $(git rev-parse --show-toplevel) resolution
  5. $CLAUDE_PROJECT_DIR / $PROJECT_ROOT resolution
  6. $VNX_HOME via the ~/.vnx-system/current symlink and via each fallback
     candidate, plus candidate-priority ordering
  7. the locally-assigned-$ROOT blind spot (documented-skip regression)
  8. the CLI/doctor coverage-gap messaging fix (unresolved != resolved)
  9. the hookpin_check.sh SessionStart wiring itself, both at the settings.json
     level (this repo + the fleet template) and end-to-end through the real
     shell script — including a regression test for the exit-code-gating bug
     (--json exits 1 on a *found* dead pin; an `|| exit 0` on that capture
     silently swallowed the positive case) found and fixed in this same PR.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import hookpin_check  # noqa: E402
from hookpin_check import (  # noqa: E402
    STATUS_MISSING,
    STATUS_OK,
    STATUS_UNRESOLVED,
    check_project_hook_pins,
)
from vnx_doctor import PASS, WARN, FAIL, check_hook_pins  # noqa: E402

HOOKPIN_SH = SCRIPT_DIR / "hooks" / "hookpin_check.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_settings(project_root: Path, hooks: dict) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}))


def _stop_hook(command: str) -> dict:
    return {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]}


# ---------------------------------------------------------------------------
# 1. Dead / live absolute pin
# ---------------------------------------------------------------------------

def test_dead_absolute_pin_reported_missing(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"bash {tmp_path}/does-not-exist.sh"))

    findings = check_project_hook_pins(project)

    assert len(findings) == 1
    assert findings[0].status == STATUS_MISSING


def test_live_absolute_pin_reported_ok(tmp_path):
    script = tmp_path / "real.sh"
    script.write_text("#!/bin/bash\n")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"bash {script}"))

    findings = check_project_hook_pins(project)

    assert len(findings) == 1
    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(script)


# ---------------------------------------------------------------------------
# 2. Absent-by-design: no .claude dir, no hooks key
# ---------------------------------------------------------------------------

def test_no_claude_directory_returns_no_findings(tmp_path):
    assert check_project_hook_pins(tmp_path) == []


def test_no_hooks_key_returns_no_findings(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": {}}))

    assert check_project_hook_pins(project) == []


# ---------------------------------------------------------------------------
# 3. Disabled matcher
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("matcher", ["disabled", "DISABLED", "__never_match__"])
def test_disabled_matcher_is_skipped(tmp_path, matcher):
    project = tmp_path / "proj"
    _write_settings(project, {
        "PreToolUse": [{
            "matcher": matcher,
            "hooks": [{"type": "command", "command": f"bash {tmp_path}/does-not-exist.sh"}],
        }]
    })

    assert check_project_hook_pins(project) == []


# ---------------------------------------------------------------------------
# 4. $(git rev-parse --show-toplevel) resolution
# ---------------------------------------------------------------------------

def test_git_toplevel_token_resolves_against_project_root(tmp_path):
    real = tmp_path / "proj" / "scripts" / "hooks" / "real.sh"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/bash\n")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(
        "bash -c 'exec bash \"$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
        "/scripts/hooks/real.sh\"'"
    ))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(real)
    assert findings[0].raw_path.startswith("$(git rev-parse --show-toplevel)")


def test_git_toplevel_token_reports_dead_when_missing(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(
        "bash -c 'exec bash \"$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
        "/scripts/hooks/gone.sh\"'"
    ))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_MISSING


# ---------------------------------------------------------------------------
# 5. $CLAUDE_PROJECT_DIR / $PROJECT_ROOT resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", ["CLAUDE_PROJECT_DIR", "PROJECT_ROOT"])
def test_project_relative_vars_resolve(tmp_path, var):
    real = tmp_path / "proj" / "scripts" / "real.py"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"python3 ${var}/scripts/real.py"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(real)


@pytest.mark.parametrize("var", ["CLAUDE_PROJECT_DIR", "PROJECT_ROOT"])
def test_project_relative_vars_braced_form_resolves(tmp_path, var):
    real = tmp_path / "proj" / "scripts" / "real.py"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook("python3 ${%s}/scripts/real.py" % var))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(real)


# ---------------------------------------------------------------------------
# 6. $VNX_HOME resolution: symlink, each fallback candidate, and priority
# ---------------------------------------------------------------------------

def test_vnx_home_resolves_via_current_symlink(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_HOME", raising=False)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    version_dir = tmp_path / "versions" / "1.4.2"
    (version_dir / "scripts" / "hooks").mkdir(parents=True)
    (version_dir / "scripts" / "hooks" / "foo.sh").write_text("x")
    vnx_system = fake_home / ".vnx-system"
    vnx_system.mkdir()
    current = vnx_system / "current"
    current.symlink_to(version_dir)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project = tmp_path / "proj"
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(current / "scripts" / "hooks" / "foo.sh")


def test_vnx_home_resolves_via_env_var(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    (engine / "scripts" / "hooks").mkdir(parents=True)
    (engine / "scripts" / "hooks" / "foo.sh").write_text("x")
    monkeypatch.setenv("VNX_HOME", str(engine))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(engine / "scripts" / "hooks" / "foo.sh")


def test_vnx_home_resolves_via_project_dot_vnx(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    (project / ".vnx" / "scripts" / "hooks").mkdir(parents=True)
    (project / ".vnx" / "scripts" / "hooks" / "foo.sh").write_text("x")
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(project / ".vnx" / "scripts" / "hooks" / "foo.sh")


def test_vnx_home_resolves_via_project_claude_vnx_system(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    (project / ".claude" / "vnx-system" / "scripts" / "hooks").mkdir(parents=True)
    (project / ".claude" / "vnx-system" / "scripts" / "hooks" / "foo.sh").write_text("x")
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(
        project / ".claude" / "vnx-system" / "scripts" / "hooks" / "foo.sh"
    )


def test_vnx_home_resolves_via_project_root_itself(tmp_path, monkeypatch):
    """Standalone-dev checkout: VNX_HOME == PROJECT_ROOT, the last-resort candidate."""
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    (project / "scripts" / "hooks").mkdir(parents=True)
    (project / "scripts" / "hooks" / "foo.sh").write_text("x")
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_OK
    assert findings[0].resolved_path == str(project / "scripts" / "hooks" / "foo.sh")


def test_vnx_home_candidate_priority_env_before_dot_vnx(tmp_path, monkeypatch):
    """Two candidates both satisfy the token; the env var must win (first in
    _vnx_home_candidates()'s order), not project_root/.vnx."""
    env_dir = tmp_path / "env-engine"
    (env_dir / "scripts" / "hooks").mkdir(parents=True)
    (env_dir / "scripts" / "hooks" / "foo.sh").write_text("env")
    monkeypatch.setenv("VNX_HOME", str(env_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    (project / ".vnx" / "scripts" / "hooks").mkdir(parents=True)
    (project / ".vnx" / "scripts" / "hooks" / "foo.sh").write_text("dotvnx")
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].resolved_path == str(env_dir / "scripts" / "hooks" / "foo.sh")


def test_vnx_home_unresolved_when_no_candidate_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-vnx-system-here")

    project = tmp_path / "proj"
    _write_settings(project, _stop_hook("bash ${VNX_HOME}/scripts/hooks/foo.sh"))

    findings = check_project_hook_pins(project)

    assert findings[0].status == STATUS_UNRESOLVED


# ---------------------------------------------------------------------------
# 7. Locally-assigned $ROOT blind spot — documented-skip regression
# ---------------------------------------------------------------------------

def test_locally_assigned_root_var_is_unresolved_not_silently_ok_or_dead(tmp_path):
    """OI-1123 r2 audit finding 2: resolve_token() only knows CLAUDE_PROJECT_DIR,
    PROJECT_ROOT and VNX_HOME. A hook that assigns ROOT=$(git rev-parse
    --show-toplevel) *locally* and references $ROOT later in the SAME command
    string is invisible to this checker — it has no shell parser and cannot
    see an assignment made earlier in the same string. This is the exact
    shape of 3 of vnx-orchestration's own 15 SessionStart/SessionEnd/Stop
    hook entries today (build_current_state.py, build_doc_indexes.py,
    session_stop_rotation.py). The design choice (report UNRESOLVED, never
    guess OK or MISSING) is documented in the module docstring; this test
    pins that current behavior so a future change is deliberate, not an
    accidental regression that starts producing false PASSes or false FAILs.
    """
    real = tmp_path / "proj" / "scripts" / "foo.py"
    real.parent.mkdir(parents=True)
    real.write_text("x")  # the file DOES exist — proves this isn't a MISSING misdetect
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(
        "bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && "
        "python3 \"$ROOT/scripts/foo.py\"'"
    ))

    findings = check_project_hook_pins(project)

    assert len(findings) == 1
    assert findings[0].status == STATUS_UNRESOLVED, (
        "locally-assigned $ROOT must stay UNRESOLVED (not silently OK, not "
        "falsely DEAD) — see module docstring for the reasoning"
    )


# ---------------------------------------------------------------------------
# 8. CLI (main()) and vnx_doctor coverage-gap messaging
# ---------------------------------------------------------------------------

def _run_cli(monkeypatch, capsys, project_root):
    monkeypatch.setattr(sys, "argv", ["hookpin_check.py", "--project-root", str(project_root)])
    rc = hookpin_check.main()
    return rc, capsys.readouterr().out


def test_cli_all_resolve_states_full_coverage(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real.sh"
    real.write_text("x")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"bash {real}"))

    rc, out = _run_cli(monkeypatch, capsys, project)

    assert rc == 0
    assert "All 1 configured hook pin(s) resolve" in out


def test_cli_unresolved_only_does_not_claim_full_coverage(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(
        "bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && bash \"$ROOT/x.sh\"'"
    ))

    rc, out = _run_cli(monkeypatch, capsys, project)

    assert rc == 0, "unresolved alone must not fail the check (design: never guess)"
    assert "unresolved" in out
    assert "not confirmed dead" in out
    assert "All 1 configured hook pin(s) resolve" not in out, (
        "must not claim full coverage when a pin was never actually checked"
    )


def test_cli_dead_and_unresolved_reported_together(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    _write_settings(project, {
        "Stop": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": f"bash {tmp_path}/gone.sh"},
            ]},
            {"matcher": "", "hooks": [
                {"type": "command", "command":
                 "bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && bash \"$ROOT/x.sh\"'"},
            ]},
        ]
    })

    rc, out = _run_cli(monkeypatch, capsys, project)

    assert rc == 1
    assert "1 dead hook pin(s)" in out
    assert "1 more pin(s) unresolved" in out


def test_check_hook_pins_pass_when_all_resolve(tmp_path):
    real = tmp_path / "real.sh"
    real.write_text("x")
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"bash {real}"))

    results = check_hook_pins({"PROJECT_ROOT": str(project)})

    assert len(results) == 1
    assert results[0].status == PASS


def test_check_hook_pins_fail_when_dead_pin_present(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(f"bash {tmp_path}/gone.sh"))

    results = check_hook_pins({"PROJECT_ROOT": str(project)})

    assert len(results) == 1
    assert results[0].status == FAIL
    assert "do not resolve" in results[0].message


def test_check_hook_pins_warn_when_only_unresolved(tmp_path):
    """Finding 2 fix: an unresolved-only result must not read as a clean PASS
    — that would claim full coverage of pins this checker never actually
    verified."""
    project = tmp_path / "proj"
    _write_settings(project, _stop_hook(
        "bash -c 'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && bash \"$ROOT/x.sh\"'"
    ))

    results = check_hook_pins({"PROJECT_ROOT": str(project)})

    assert len(results) == 1
    assert results[0].status == WARN
    assert "could not be checked" in results[0].message


def test_check_hook_pins_empty_when_no_settings_file(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()

    assert check_hook_pins({"PROJECT_ROOT": str(project)}) == []


# ---------------------------------------------------------------------------
# 9a. SessionStart wiring — settings.json / template, both must reference it
# ---------------------------------------------------------------------------

def test_repo_settings_json_wires_hookpin_check_into_sessionstart():
    """OI-1123 finding 1: without this wire, `vnx doctor` (pull, human-
    triggered) was the ONLY live detection — the central claim that a dead
    pin becomes loud on the very next session was not true."""
    settings_path = REPO / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    commands = [
        h.get("command", "")
        for e in data.get("hooks", {}).get("SessionStart", [])
        for h in e.get("hooks", [])
    ]
    assert any("hookpin_check.sh" in c for c in commands)


def test_fleet_template_wires_hookpin_check_into_sessionstart():
    """Same finding, fleet side: without this, `vnx bootstrap-hooks`/`vnx init`
    never deploys the check to mission-control, SEOcrawler_v2, sales-copilot."""
    tmpl_path = REPO / "templates" / "settings_vnx_keys.json.tmpl"
    data = json.loads(tmpl_path.read_text())
    commands = [
        h.get("command", "")
        for e in data.get("hooks", {}).get("SessionStart", [])
        for h in e.get("hooks", [])
    ]
    assert any("hookpin_check.sh" in c for c in commands)


# ---------------------------------------------------------------------------
# 9b. hookpin_check.sh end-to-end (real subprocess, real script)
# ---------------------------------------------------------------------------

def test_hookpin_check_sh_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(HOOKPIN_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_hookpin_check_sh_surfaces_dead_pin_as_sessionstart_json(tmp_path):
    """Regression test for the bug found while proving the SessionStart wire:
    --json mode exits 1 exactly when it finds a dead pin (the positive
    case), so gating the RESULT capture with `|| exit 0` silently swallowed
    the one case this hook exists to report. Runs the REAL script."""
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = {"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"bash {tmp_path}/does-not-exist.sh"}
    ]}]}}
    (project / ".claude" / "settings.json").write_text(json.dumps(settings))

    result = subprocess.run(
        ["bash", str(HOOKPIN_SH)], cwd=str(project),
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, "fail-soft contract: hook must always exit 0"
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "does-not-exist.sh" in ctx
    assert "do NOT resolve" in ctx


def test_hookpin_check_sh_silent_when_all_pins_resolve(tmp_path):
    real = tmp_path / "real.sh"
    real.write_text("#!/bin/bash\n")
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = {"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"bash {real}"}
    ]}]}}
    (project / ".claude" / "settings.json").write_text(json.dumps(settings))

    result = subprocess.run(
        ["bash", str(HOOKPIN_SH)], cwd=str(project),
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hookpin_check_sh_noop_without_settings_file(tmp_path):
    result = subprocess.run(
        ["bash", str(HOOKPIN_SH)], cwd=str(tmp_path),
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
