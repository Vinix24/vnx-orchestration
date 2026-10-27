"""tests/test_settings_merge_hook_ownership.py — OI-2057 (track fleet-release-and-rollout PR-1).

MEASURED 2026-10-27 on main ``d6e71d16``: ``scripts/vnx_settings_merge.py``
``merge_settings`` carried ``# Replace hooks (VNX-owned entirely)`` and assigned
``result["hooks"] = vnx_template["hooks"]``. Every project hook in a
``.claude/settings.json`` therefore disappeared on ``vnx regen-settings
--merge`` — the command ``scripts/hooks/hookpin_check.sh`` advises on a hook
problem, and step 4 of the per-project cutover in
``docs/operations/install-central-runbook.md``. A pinned consumer measured the
same day carried 10 hook handlers, 3 VNX-driven and 7 project-owned (among them
the ``t0-readonly-enforcer.sh`` security hook); a merge would have deleted the 7
and nobody would have noticed. Nothing called ``merge_settings`` in a test, so
the defect could sit there.

THE RULE (``is_vnx_owned_hook``), which these tests pin. A hook handler is
VNX-owned when its command, whitespace-normalised, either

  a) equals a command the VNX template renders for the same event — the
     template is the merge's own VNX-owned set (``_vnx_meta.managed_keys``
     declares ``hooks`` VNX-owned); judged per event and ignoring the matcher,
     because the documented repair for a dead matcher is a plain ``--merge``
     and a matcher change must not leave the old entry behind next to the new
     one; or
  b) names a path inside a VNX install's hook tree: a ``scripts/hooks/`` path
     segment, or anything under the central install (``/.vnx-system/``, which
     covers ``~/.vnx-system/current/...``, ``$HOME/.vnx-system/versions/v1.7.1/...``
     and the two brace forms).

Measured against the consumer above: its 3 VNX handlers name
``~/.vnx-system/current/scripts/hooks/t0_context_guard.py``; its 7 project
handlers name the project's own ``.claude/hooks/`` or
``.claude/vnx-system/hooks/`` — note ``hooks/`` without the ``scripts/`` prefix,
so the legacy embedded install's project-local hook directory stays
project-owned (the older copy of this very module's home). A handler whose
command is missing, ``None``, not a string or blank establishes no VNX
ownership and is KEPT: keeping a hook loses nothing, replacing it can delete a
project hook.

Everything here runs against a temporary project directory; no real
``settings.json``, ``~/.claude`` or install tree is written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vnx_settings_merge as engine  # noqa: E402

STALE_VNX_COMMAND = (
    "bash /home/tester/.vnx-system/versions/v1.7.1/scripts/hooks/"
    "pretooluse_block_raw_claude_spawn.sh"
)
PROJECT_COMMAND = "$CLAUDE_PROJECT_DIR/.claude/hooks/t0-readonly-enforcer.sh"


def _settings_with_one_vnx_and_one_project_hook() -> dict:
    """One stale VNX hook and one project hook on the SAME event (PreToolUse)."""
    return {
        "env": {"VNX_CONTEXT_ROTATION_ENABLED": "0", "PROJECT_FLAG": "1"},
        "permissions": {"allow": ["Bash(ls)"], "deny": [], "ask": ["Bash(git push*)"]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": STALE_VNX_COMMAND, "timeout": 5}],
                },
                {
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": PROJECT_COMMAND, "timeout": 5}],
                },
            ]
        },
        "_vnx_meta": {"version": "1.0", "managed_keys": ["hooks"]},
    }


def _write_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    return project


def _write_settings(project: Path, data: dict) -> Path:
    path = project / ".claude" / "settings.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _run_cli(project: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "vnx_settings_merge.py"), *args,
         "--project-root", str(project), "--vnx-home", str(REPO_ROOT)],
        capture_output=True, text=True, env=env,
    )


def _handlers(hooks: dict) -> List[dict]:
    out: List[dict] = []
    for groups in hooks.values():
        for group in groups:
            out.extend(group.get("hooks", []))
    return out


def _commands(hooks: dict) -> List[str]:
    return [handler.get("command") for handler in _handlers(hooks)]


def _event_handlers(hooks: dict, event: str) -> List[dict]:
    out: List[dict] = []
    for group in hooks.get(event, []):
        out.extend(group.get("hooks", []))
    return out


# ─────────────────────────────────────────────────────────────────────────
# The merge itself
# ─────────────────────────────────────────────────────────────────────────


def test_merge_keeps_the_project_hook_and_refreshes_the_vnx_hook(tmp_path):
    """The primary behaviour: same event, one VNX hook and one project hook."""
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    merged = engine.merge_settings(_settings_with_one_vnx_and_one_project_hook(), template)

    commands = _commands(merged["hooks"])
    assert PROJECT_COMMAND in commands, "the project hook must survive --merge"
    assert STALE_VNX_COMMAND not in commands, "the stale VNX hook must be refreshed"
    refreshed = f"bash {REPO_ROOT}/scripts/hooks/pretooluse_block_raw_claude_spawn.sh"
    assert refreshed in commands
    for template_command in _commands(template["hooks"]):
        assert template_command in commands


def test_merge_preserves_the_project_group_around_its_hook(tmp_path):
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    merged = engine.merge_settings(_settings_with_one_vnx_and_one_project_hook(), template)

    project_groups = [
        group for group in merged["hooks"]["PreToolUse"]
        if any(handler.get("command") == PROJECT_COMMAND for handler in group.get("hooks", []))
    ]
    assert len(project_groups) == 1
    assert project_groups[0]["matcher"] == "Write|Edit"


def test_a_vnx_hook_the_template_no_longer_renders_is_dropped(tmp_path):
    """PostToolUse carries only a VNX-tree hook; the template has no PostToolUse."""
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"]["PostToolUse"] = [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 /opt/.vnx-system/current/scripts/hooks/gone.py"},
        ]},
    ]

    merged, report = engine.merge_settings_with_report(existing, template)

    assert "PostToolUse" not in merged["hooks"]
    replaced = report["PostToolUse"]["replaced"]
    assert len(replaced) == 1
    assert replaced[0]["new_command"] is None


@pytest.mark.parametrize("handler", [
    {},
    {"command": None},
    {"command": 12},
    {"command": ""},
    {"command": "   "},
])
def test_handler_without_a_usable_command_is_kept(tmp_path, handler):
    """Fail-closed on the ownership evidence: no command, no VNX claim, no deletion."""
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [dict(handler)]})

    merged = engine.merge_settings(existing, template)

    assert len(_commands(merged["hooks"])) == len(_commands(template["hooks"])) + 2


def test_a_malformed_group_is_kept_and_fails_validation(tmp_path):
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"]["PreToolUse"].append("not-a-group")

    merged = engine.merge_settings(existing, template)

    assert "not-a-group" in merged["hooks"]["PreToolUse"]
    assert any("must be an object" in issue for issue in engine.validate_settings(merged))


@pytest.mark.parametrize("hooks", [
    [],
    "nope",
    {"PreToolUse": {"matcher": "Bash"}},
    {"PreToolUse": None},
])
def test_an_unmergeable_hooks_shape_is_refused(tmp_path, capsys, hooks):
    """An unreadable hooks tree is no evidence that its handlers are VNX-owned."""
    project = _write_project(tmp_path)
    template = engine.load_template(str(REPO_ROOT), str(project))
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"] = hooks

    with pytest.raises(SystemExit) as raised:
        engine.merge_settings(existing, template)

    assert raised.value.code == 1
    assert "refusing to touch its hooks" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────
# The ownership rule, measured
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "bash /home/tester/.vnx-system/current/scripts/hooks/pretooluse_block_raw_claude_spawn.sh",
    "python3 ~/.vnx-system/versions/v1.7.1/scripts/hooks/t0_context_guard.py",
    'bash -c \'VNX_HOME="${VNX_HOME:-$HOME/.vnx-system/current}"; exec bash '
    '"${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh"\'',
    "bash /home/tester/proj/.vnx/scripts/hooks/hookpin_check.sh",
    "bash /home/tester/proj/.claude/vnx-system/scripts/hooks/hookpin_check.sh",
    'bash -c \'ROOT=$(git rev-parse --show-toplevel); exec python3 "$ROOT/scripts/hooks/t0_context_guard.py"\'',
])
def test_is_vnx_owned_hook_recognises_the_vnx_hook_tree(command):
    assert engine.is_vnx_owned_hook({"type": "command", "command": command}, set()) is True


@pytest.mark.parametrize("command", [
    "$CLAUDE_PROJECT_DIR/.claude/hooks/t0-readonly-enforcer.sh",
    "bash /home/tester/proj/.claude/hooks/sessionstart_t0_minimal.sh",
    "bash /home/tester/proj/.claude/vnx-system/hooks/vnx_context_monitor.sh",
    "bash .claude/hooks/activate_venv.sh",
    'bash -c \'exec bash "$(git rev-parse --show-toplevel)/hooks/monitor_tripwire.sh"\'',
    "/usr/local/bin/my-project-linter --hook",
])
def test_a_project_hook_is_not_vnx_owned(command):
    assert engine.is_vnx_owned_hook({"type": "command", "command": command}, set()) is False


def test_is_vnx_owned_hook_recognises_template_membership_across_matcher_drift():
    """The documented repair for a dead matcher is a plain --merge, so the event's
    template commands decide ownership even when the group's matcher changed."""
    command = "bash /home/tester/proj/.claude/hooks/sessionstart.sh"
    assert engine.is_vnx_owned_hook({"command": command}, {command}) is True


@pytest.mark.parametrize("handler", [
    {},
    {"command": None},
    {"command": 12},
    {"command": ""},
    {"command": "   "},
    {"type": "command"},
    "not-a-handler",
])
def test_no_usable_command_claims_no_vnx_ownership(handler):
    assert engine.is_vnx_owned_hook(handler, set()) is False


def test_whitespace_differences_do_not_hide_a_template_hook():
    command = "bash  /home/tester/proj/.claude/hooks/sessionstart.sh   "
    assert engine.is_vnx_owned_hook({"command": command},
                                    {"bash /home/tester/proj/.claude/hooks/sessionstart.sh"}) is True


# ─────────────────────────────────────────────────────────────────────────
# The CLI: dry-run JSON report, --replace-hooks, and what lands on disk
# ─────────────────────────────────────────────────────────────────────────


def test_dry_run_json_reports_kept_added_and_replaced_per_event(tmp_path):
    project = _write_project(tmp_path)
    _write_settings(project, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--dry-run", "--json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    report = payload["_vnx_hook_report"]
    assert report["mode"] == "merge"

    # The project hook is still in the settings the dry run shows.
    assert PROJECT_COMMAND in _commands(payload["hooks"])

    pre = report["PreToolUse"]
    assert [entry["command"] for entry in pre["kept"]] == [PROJECT_COMMAND]
    assert [entry["old_command"] for entry in pre["replaced"]] == [STALE_VNX_COMMAND]
    assert [entry["new_command"] for entry in pre["replaced"]] == [
        f"bash {REPO_ROOT}/scripts/hooks/pretooluse_block_raw_claude_spawn.sh"
    ]
    added = [entry["command"] for entry in pre["added"]]
    assert any("pretooluse_t0_no_subagents.py" in command for command in added)
    assert PROJECT_COMMAND not in added


def test_replace_hooks_reproduces_the_old_replace_everything_result(tmp_path):
    project = _write_project(tmp_path)
    settings_path = _write_settings(project, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--replace-hooks", "--no-backup")

    assert proc.returncode == 0, proc.stderr
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    template = engine.load_template(str(REPO_ROOT), str(project))
    assert written["hooks"] == template["hooks"]
    assert PROJECT_COMMAND not in _commands(written["hooks"])


def test_merge_writes_project_and_vnx_hooks_and_no_report_into_settings(tmp_path):
    project = _write_project(tmp_path)
    settings_path = _write_settings(project, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--no-backup")

    assert proc.returncode == 0, proc.stderr
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = _commands(written["hooks"])
    assert PROJECT_COMMAND in commands
    assert STALE_VNX_COMMAND not in commands
    assert "_vnx_hook_report" not in written
    assert written["permissions"]["ask"] == ["Bash(git push*)"]
    assert written["env"]["PROJECT_FLAG"] == "1"


def test_replace_hooks_is_refused_outside_merge(tmp_path):
    project = _write_project(tmp_path)
    proc = _run_cli(project, tmp_path, "--full", "--replace-hooks", "--dry-run")
    assert proc.returncode != 0
    assert "--replace-hooks" in proc.stderr
