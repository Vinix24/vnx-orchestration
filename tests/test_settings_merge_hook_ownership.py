"""tests/test_settings_merge_hook_ownership.py — OI-2057 (track fleet-release-and-rollout PR-1).

MEASURED 2026-10-27 on main ``d6e71d16``: ``vnx_settings_merge.merge_settings``
carried ``# Replace hooks (VNX-owned entirely)`` and assigned
``result["hooks"] = vnx_template["hooks"]``. Every project hook in a consumer's
``.claude/settings.json`` therefore disappeared on ``vnx regen-settings
--merge`` — the command ``scripts/hooks/hookpin_check.sh`` advises on a hook
problem, and step 4 of the per-project cutover. A pinned consumer measured the
same day carried 10 handlers: 3 VNX-driven, 7 project-owned (one of them its
``t0-readonly-enforcer.sh`` security hook). No test called ``merge_settings``.

THE RULE these tests pin: see ``is_vnx_owned_hook`` in the module for the rule
and its measurement. A handler with no readable command claims no VNX ownership
and is kept — keeping deletes nothing, refreshing on a guess deletes a project
hook. Everything here runs against a temporary project directory; no real
``settings.json``, ``~/.claude`` or install tree is written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List

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
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": STALE_VNX_COMMAND, "timeout": 5}]},
            {"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": PROJECT_COMMAND, "timeout": 5}]},
        ]},
        "_vnx_meta": {"version": "1.0", "managed_keys": ["hooks"]},
    }


def _write_settings(tmp_path: Path, data: dict) -> tuple[Path, Path]:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    path = project / ".claude" / "settings.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return project, path


def _template(project: Path) -> dict:
    return engine.load_template(str(REPO_ROOT), str(project))


def _run_cli(project: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "vnx_settings_merge.py"), *args,
         "--project-root", str(project), "--vnx-home", str(REPO_ROOT)],
        capture_output=True, text=True, env=env,
    )


def _handlers(hooks: dict) -> List[dict]:
    return [handler for groups in hooks.values() for group in groups
            for handler in group.get("hooks", [])]


def _commands(hooks: dict) -> List[str]:
    return [handler.get("command") for handler in _handlers(hooks)]


# ── The merge itself ──────────────────────────────────────────────────────


def test_merge_keeps_the_project_hook_and_refreshes_the_vnx_hook(tmp_path):
    """The primary behaviour: same event, one VNX hook and one project hook."""
    project = tmp_path / "project"
    project.mkdir()
    template = _template(project)
    merged = engine.merge_settings(_settings_with_one_vnx_and_one_project_hook(), template)

    commands = _commands(merged["hooks"])
    assert PROJECT_COMMAND in commands, "the project hook must survive --merge"
    assert STALE_VNX_COMMAND not in commands, "the stale VNX hook must be refreshed"
    refreshed = f"bash {REPO_ROOT}/scripts/hooks/pretooluse_block_raw_claude_spawn.sh"
    assert refreshed in commands
    for template_command in _commands(template["hooks"]):
        assert template_command in commands

    project_groups = [group for group in merged["hooks"]["PreToolUse"]
                      if PROJECT_COMMAND in [h.get("command") for h in group["hooks"]]]
    assert [group["matcher"] for group in project_groups] == ["Write|Edit"]


def test_a_vnx_hook_the_template_no_longer_renders_is_dropped(tmp_path):
    """PostToolUse carries only a VNX-tree hook; the template has no PostToolUse."""
    project = tmp_path / "project"
    project.mkdir()
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"]["PostToolUse"] = [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /opt/.vnx-system/current/scripts/hooks/gone.py"}]}]

    merged, report = engine.merge_settings_with_report(existing, _template(project))

    assert "PostToolUse" not in merged["hooks"]
    assert [entry["new_command"] for entry in report["PostToolUse"]["replaced"]] == [None]


def test_a_malformed_group_is_kept_and_fails_validation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"]["PreToolUse"].append("not-a-group")

    merged = engine.merge_settings(existing, _template(project))

    assert "not-a-group" in merged["hooks"]["PreToolUse"]
    assert any("must be an object" in issue for issue in engine.validate_settings(merged))


@pytest.mark.parametrize("hooks", [[], "nope", {"PreToolUse": {"matcher": "Bash"}}, {"PreToolUse": None}])
def test_an_unmergeable_hooks_shape_is_refused(tmp_path, capsys, hooks):
    """An unreadable hooks tree is no evidence that its handlers are VNX-owned."""
    project = tmp_path / "project"
    project.mkdir()
    existing = _settings_with_one_vnx_and_one_project_hook()
    existing["hooks"] = hooks

    with pytest.raises(SystemExit) as raised:
        engine.merge_settings(existing, _template(project))

    assert raised.value.code == 1
    assert "refusing to touch its hooks" in capsys.readouterr().err


# ── The ownership rule, measured ──────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "bash /home/tester/.vnx-system/current/scripts/hooks/pretooluse_block_raw_claude_spawn.sh",
    "python3 ~/.vnx-system/versions/v1.7.1/scripts/hooks/t0_context_guard.py",
    'bash -c \'VNX_HOME="${VNX_HOME:-$HOME/.vnx-system/current}"; exec bash '
    '"${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh"\'',
    "bash /home/tester/proj/.vnx/scripts/hooks/hookpin_check.sh",
    'bash -c \'ROOT=$(git rev-parse --show-toplevel); exec python3 "$ROOT/scripts/hooks/t0_context_guard.py"\'',
])
def test_is_vnx_owned_hook_recognises_the_vnx_hook_tree(command):
    assert engine.is_vnx_owned_hook({"type": "command", "command": command}, set()) is True


@pytest.mark.parametrize("command", [
    PROJECT_COMMAND,
    "bash /home/tester/proj/.claude/hooks/sessionstart_t0_minimal.sh",
    "bash /home/tester/proj/.claude/vnx-system/hooks/vnx_context_monitor.sh",
    'bash -c \'exec bash "$(git rev-parse --show-toplevel)/hooks/monitor_tripwire.sh"\'',
])
def test_a_project_hook_is_not_vnx_owned(command):
    assert engine.is_vnx_owned_hook({"type": "command", "command": command}, set()) is False


def test_is_vnx_owned_hook_recognises_template_membership_across_matcher_drift():
    """The documented repair for a dead matcher is a plain --merge, so the event's
    template commands decide ownership even when the group's matcher changed."""
    command = "bash /home/tester/proj/.claude/hooks/sessionstart.sh"
    assert engine.is_vnx_owned_hook({"command": command}, {command}) is True


@pytest.mark.parametrize("handler", [
    {}, {"command": None}, {"command": 12}, {"command": ""}, {"command": "   "},
    {"type": "command"}, "not-a-handler",
])
def test_no_usable_command_claims_no_vnx_ownership(handler):
    assert engine.is_vnx_owned_hook(handler, set()) is False


def test_whitespace_differences_do_not_hide_a_template_hook():
    command = "bash  /home/tester/proj/.claude/hooks/sessionstart.sh   "
    assert engine.is_vnx_owned_hook(
        {"command": command}, {"bash /home/tester/proj/.claude/hooks/sessionstart.sh"}) is True


# ── The CLI: dry-run JSON report, --replace-hooks, and what lands on disk ──


def test_dry_run_json_reports_kept_added_and_replaced_per_event(tmp_path):
    project, _ = _write_settings(tmp_path, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--dry-run", "--json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    # The project hook is still in the settings the dry run shows.
    assert PROJECT_COMMAND in _commands(payload["hooks"])
    report = payload["_vnx_hook_report"]
    assert report["mode"] == "merge"
    pre = report["PreToolUse"]
    assert [entry["command"] for entry in pre["kept"]] == [PROJECT_COMMAND]
    assert [entry["old_command"] for entry in pre["replaced"]] == [STALE_VNX_COMMAND]
    assert [entry["new_command"] for entry in pre["replaced"]] == [
        f"bash {REPO_ROOT}/scripts/hooks/pretooluse_block_raw_claude_spawn.sh"]
    added = [entry["command"] for entry in pre["added"]]
    assert any("pretooluse_t0_no_subagents.py" in command for command in added)
    assert PROJECT_COMMAND not in added


def test_replace_hooks_reproduces_the_old_replace_everything_result(tmp_path):
    project, settings_path = _write_settings(
        tmp_path, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--replace-hooks", "--no-backup")

    assert proc.returncode == 0, proc.stderr
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written["hooks"] == _template(project)["hooks"]
    assert PROJECT_COMMAND not in _commands(written["hooks"])


def test_merge_writes_project_and_vnx_hooks_and_no_report_into_settings(tmp_path):
    project, settings_path = _write_settings(
        tmp_path, _settings_with_one_vnx_and_one_project_hook())

    proc = _run_cli(project, tmp_path, "--merge", "--no-backup")

    assert proc.returncode == 0, proc.stderr
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert PROJECT_COMMAND in _commands(written["hooks"])
    assert STALE_VNX_COMMAND not in _commands(written["hooks"])
    assert "_vnx_hook_report" not in written
    assert written["permissions"]["ask"] == ["Bash(git push*)"]
    assert written["env"]["PROJECT_FLAG"] == "1"


def test_replace_hooks_is_refused_outside_merge(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    proc = _run_cli(project, tmp_path, "--full", "--replace-hooks", "--dry-run")
    assert proc.returncode != 0
    assert "--replace-hooks" in proc.stderr
