"""tests/test_sessionstart_hook_role_alarm.py: a T0 without its role says so.

Dispatch-ID: 20260924-t0-geen-subagents-en-rol-alarm

The canonical T0 role (role-orchestrator.md) reaches a Claude session only
through an ``@role-orchestrator.md`` import in the terminal's CLAUDE.md.
SEOcrawler_v2 ran T0 sessions without that import from 16-07 (operator finding,
24-09): an uncommitted edit had dropped the line, every rule the role carries
was absent, and nothing said so until the six-hourly fleet_role_drift sweep,
and then only as a beacon.

``hooks/sessionstart.sh`` now measures it at the start of every T0 session,
with fleet_role_drift's own reach axis (``--reach``), and puts a line at the
very top of the injection when the import is missing.

What these tests pin:

1. The alarm is the FIRST thing in the injection, and it appears exactly when
   the import is missing (no CLAUDE.md, or a CLAUDE.md without the line).
2. It is a warning, not a block: the rest of the injection is still delivered
   and the hook still exits 0.
3. It is T0-only: a worker terminal without an import is not a defect.
4. It works from a COPIED hook. bootstrap_hooks copies this file into
   ``<project>/.claude/hooks/``, where ``$_HOOK_DIR/../scripts`` does not exist
   (measured in sales-copilot), so the engine is looked up along the same
   candidates hookpin_check uses. An alarm that only worked where the engine
   sits next to the hook would be dead in exactly the projects that need it.
5. A check that cannot run says UNMEASURED instead of passing.

Isolation: every project is a tmp_path and every run has HOME and the central
store redirected into it. Nothing reads the real ``~/.claude``, ``~/.vnx-system``
or a real project's settings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
FLEET_ROLE_DRIFT = REPO / "scripts" / "fleet_role_drift.py"

ALARM = "ROLE NOT LOADED: deze T0 draait zonder de canonieke rol"
UNAVAILABLE = "ROLE CHECK UNAVAILABLE"
INJECTION_HEAD = "T0 Master Orchestrator Active"
SKILL_MARKER = "UNIQUE-PLAYBOOK-MARKER-9c2d71"

# What the shipped T0 CLAUDE.md looks like: prose that MENTIONS the role file
# without importing it, then the import line itself.
CLAUDE_MD_WITH_IMPORT = """\
<!--
  Thin T0 pointer. The canonical orchestrator role lives in role-orchestrator.md
  and is identical across the whole fleet.
-->

@role-orchestrator.md
"""

# The same file after the incident's edit: the import line is gone, the prose
# about the role file is still there.
CLAUDE_MD_WITHOUT_IMPORT = CLAUDE_MD_WITH_IMPORT.replace("@role-orchestrator.md\n", "")

SKILL_BODY = f"""\
---
name: t0-orchestrator
description: test skill
disable-model-invocation: true
---

# T0 Orchestrator

{SKILL_MARKER}
"""


def _env(tmp_path: Path, **extra: str) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["HOME"] = str(home)
    env["VNX_DATA_HOME"] = str(tmp_path / "vnx-data")
    env.update(extra)
    return env


def _make_project(tmp_path: Path, claude_md: str | None, *, worker_claude_md: str | None = None) -> Path:
    root = tmp_path / "project"
    (root / ".vnx").mkdir(parents=True)
    for term in ("T0", "T1", "T2", "T3"):
        (root / ".claude" / "terminals" / term).mkdir(parents=True)
    if claude_md is not None:
        (root / ".claude" / "terminals" / "T0" / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    if worker_claude_md is not None:
        for term in ("T1", "T2", "T3"):
            (root / ".claude" / "terminals" / term / "CLAUDE.md").write_text(worker_claude_md, encoding="utf-8")
    return root


def _skill(root: Path) -> None:
    skill_dir = root / ".claude" / "skills" / "t0-orchestrator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")


def _copy_hook_into(root: Path) -> Path:
    """The consumer layout: bootstrap_hooks copies the hook next to the project."""
    target = root / ".claude" / "hooks" / "sessionstart.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOK, target)
    return target


def _run(hook: Path, cwd: Path, env: dict) -> tuple[subprocess.CompletedProcess, str]:
    result = subprocess.run(
        ["bash", str(hook)], capture_output=True, text=True, cwd=str(cwd), env=env, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return result, out["hookSpecificOutput"]["additionalContext"]


def _t0(root: Path) -> Path:
    return root / ".claude" / "terminals" / "T0"


# ── the alarm, in the fabric layout (hook next to the engine) ────────────────


def test_missing_import_puts_the_alarm_at_the_very_top(tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    _, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    assert ctx.startswith(ALARM), ctx[:200]


def test_the_alarm_names_the_file_and_the_line_to_restore(tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    _, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    alarm = ctx.split(INJECTION_HEAD)[0]
    assert str(_t0(root) / "CLAUDE.md") in alarm
    assert "@role-orchestrator.md" in alarm


def test_absent_claude_md_is_the_same_alarm(tmp_path):
    root = _make_project(tmp_path, None)
    _, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    assert ctx.startswith(ALARM)


def test_the_incident_replay_import_removed_from_a_working_claude_md(tmp_path):
    """Same project, same hook: green with the line, alarm once the line is gone."""
    root = _make_project(tmp_path, CLAUDE_MD_WITH_IMPORT)
    env = _env(tmp_path)

    _, before = _run(HOOK, _t0(root), env)
    assert ALARM not in before

    (_t0(root) / "CLAUDE.md").write_text(CLAUDE_MD_WITHOUT_IMPORT, encoding="utf-8")
    _, after = _run(HOOK, _t0(root), env)
    assert after.startswith(ALARM)


def test_with_the_import_there_is_no_alarm_and_the_injection_opens_as_before(tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITH_IMPORT)
    _, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    assert ctx.startswith(INJECTION_HEAD), ctx[:200]
    assert ALARM not in ctx
    assert UNAVAILABLE not in ctx


def test_prose_that_mentions_the_role_file_without_importing_it_is_not_an_import(tmp_path):
    """The shipped CLAUDE.md talks ABOUT role-orchestrator.md. Only the @-line counts."""
    assert "role-orchestrator.md" in CLAUDE_MD_WITHOUT_IMPORT
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    _, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    assert ctx.startswith(ALARM)


# ── a warning, not a block ────────────────────────────────────────────────────


def test_the_alarm_does_not_stop_the_rest_of_the_injection(tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    _skill(root)
    result, ctx = _run(HOOK, _t0(root), _env(tmp_path))
    assert result.returncode == 0
    assert ctx.startswith(ALARM)
    assert INJECTION_HEAD in ctx
    assert SKILL_MARKER in ctx, "the playbook must still be delivered next to the alarm"


# ── T0 only ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("terminal", ["T1", "T2", "T3"])
def test_a_worker_terminal_without_an_import_is_not_alarmed(terminal, tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITH_IMPORT, worker_claude_md="worker notes\n")
    _, ctx = _run(HOOK, root / ".claude" / "terminals" / terminal, _env(tmp_path))
    assert ALARM not in ctx and UNAVAILABLE not in ctx


# ── from a copied hook (the consumer layout) ─────────────────────────────────


def _engine_via_vnx_home(root: Path, tmp_path: Path) -> dict:
    return _env(tmp_path, VNX_HOME=str(REPO))


def _engine_via_central_current(root: Path, tmp_path: Path) -> dict:
    env = _env(tmp_path)
    vnx_system = Path(env["HOME"]) / ".vnx-system"
    vnx_system.mkdir(parents=True)
    (vnx_system / "current").symlink_to(REPO)
    return env


def _engine_via_project_vnx_system_link(root: Path, tmp_path: Path) -> dict:
    (root / ".claude" / "vnx-system").symlink_to(REPO)
    return _env(tmp_path)


def _engine_via_project_dot_vnx(root: Path, tmp_path: Path) -> dict:
    scripts = root / ".vnx" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(FLEET_ROLE_DRIFT, scripts / "fleet_role_drift.py")
    return _env(tmp_path)


ENGINE_LOCATORS = [
    pytest.param(_engine_via_vnx_home, id="VNX_HOME"),
    pytest.param(_engine_via_central_current, id="home-.vnx-system-current"),
    pytest.param(_engine_via_project_vnx_system_link, id="project-.claude-vnx-system"),
    pytest.param(_engine_via_project_dot_vnx, id="project-.vnx-scripts"),
]


def test_the_copied_hook_cannot_see_the_engine_through_its_own_location(tmp_path):
    """The premise of the locator: next to a copied hook there is no engine."""
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    hook = _copy_hook_into(root)
    assert not (hook.parent.parent / "scripts" / "fleet_role_drift.py").exists()


@pytest.mark.parametrize("locate", ENGINE_LOCATORS)
def test_a_copied_hook_still_raises_the_alarm(locate, tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITHOUT_IMPORT)
    hook = _copy_hook_into(root)
    env = locate(root, tmp_path)
    _, ctx = _run(hook, _t0(root), env)
    assert ctx.startswith(ALARM), ctx[:300]
    assert UNAVAILABLE not in ctx


@pytest.mark.parametrize("locate", ENGINE_LOCATORS)
def test_a_copied_hook_is_quiet_when_the_import_is_there(locate, tmp_path):
    root = _make_project(tmp_path, CLAUDE_MD_WITH_IMPORT)
    hook = _copy_hook_into(root)
    env = locate(root, tmp_path)
    _, ctx = _run(hook, _t0(root), env)
    assert ctx.startswith(INJECTION_HEAD), ctx[:300]
    assert ALARM not in ctx and UNAVAILABLE not in ctx


# ── a check that cannot run says so ──────────────────────────────────────────


@pytest.mark.parametrize(
    "claude_md",
    [pytest.param(CLAUDE_MD_WITH_IMPORT, id="import-present"), pytest.param(CLAUDE_MD_WITHOUT_IMPORT, id="import-missing")],
)
def test_an_unreachable_engine_is_unmeasured_not_a_pass(claude_md, tmp_path):
    root = _make_project(tmp_path, claude_md)
    hook = _copy_hook_into(root)
    _, ctx = _run(hook, _t0(root), _env(tmp_path))
    assert ctx.startswith(UNAVAILABLE), ctx[:300]
    assert "UNMEASURED, not zero" in ctx.split(INJECTION_HEAD)[0]
    assert ALARM not in ctx, "without a measurement the hook must not claim the role is missing"
    assert str(_t0(root) / "CLAUDE.md") in ctx.split(INJECTION_HEAD)[0]
