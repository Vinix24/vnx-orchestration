"""tests/test_t0_context_guard.py — the T0 context guard hook (dispatch 20260925-t0-context-rotation-enforced).

Every test runs the real hook script as a subprocess with a hook payload on stdin and a
synthetic transcript, in an environment built from scratch: the test process itself may run
inside a dispatch (VNX_DISPATCH_ID set), which would make every call a worker no-op.
Rotation state and receipts go to tmp paths via VNX_T0_ROTATION_STATE_DIR and
VNX_T0_ROTATION_RECEIPTS_FILE, never to a real store.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "hooks" / "t0_context_guard.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_rotation_state as rotation_state

SESSION = "sess-abc-123"
PANE = "%42"


def _transcript(path: Path, tokens: int) -> Path:
    entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 1, "cache_read_input_tokens": tokens - 1, "cache_creation_input_tokens": 0,
        "output_tokens": 50}}}
    path.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def env_base(tmp_path) -> Dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "VNX_TERMINAL": "T0",
        "TMUX_PANE": PANE,
        "VNX_T0_ROTATION_STATE_DIR": str(tmp_path / "state"),
        "VNX_T0_ROTATION_RECEIPTS_FILE": str(tmp_path / "receipts" / "t0_receipts.ndjson"),
    }


def _run(tmp_path: Path, env: Dict[str, str], event: str, tokens: Optional[int], *,
         cwd: Optional[Path] = None, **extra) -> Optional[dict]:
    project = cwd or (tmp_path / "project")
    project.mkdir(parents=True, exist_ok=True)
    transcript = _transcript(tmp_path / "t.jsonl", tokens) if tokens is not None else tmp_path / "none.jsonl"
    payload = {"hook_event_name": event, "session_id": SESSION, "cwd": str(project),
               "transcript_path": str(transcript)}
    payload.update(extra)
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), env=env,
                          cwd=project, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _bash_pre(tmp_path, env, tokens, command, **kw):
    return _run(tmp_path, env, "PreToolUse", tokens, tool_name="Bash",
                tool_input={"command": command}, **kw)


def _denied(decision: Optional[dict]) -> bool:
    return bool(decision) and decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def _receipts(env) -> list:
    path = Path(env["VNX_T0_ROTATION_RECEIPTS_FILE"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── bands ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse", "Stop"])
def test_below_warn_is_silent(tmp_path, env_base, event):
    assert _run(tmp_path, env_base, event, 399_999, tool_name="Bash",
                tool_input={"command": "vnx dispatch 20260925-x"}) is None


def test_unmeasurable_transcript_is_silent(tmp_path, env_base):
    assert _run(tmp_path, env_base, "Stop", None) is None


def test_warn_injects_additional_context_on_prompt(tmp_path, env_base):
    decision = _run(tmp_path, env_base, "UserPromptSubmit", 426_361, prompt="ga door")
    ctx = decision["hookSpecificOutput"]
    assert ctx["hookEventName"] == "UserPromptSubmit"
    assert "426.361" in ctx["additionalContext"]
    assert "Rond af en bereid de rotatie voor" in ctx["additionalContext"]
    assert "decision" not in decision


def test_warn_does_not_block_tools_or_stop(tmp_path, env_base):
    assert _bash_pre(tmp_path, env_base, 450_000, "vnx dispatch 20260925-x") is None
    assert _run(tmp_path, env_base, "Stop", 450_000) is None


def test_force_blocks_vnx_dispatch_and_gh_pr_create(tmp_path, env_base):
    for command in ("vnx dispatch 20260925-x",
                    "cd /repo && bin/vnx dispatch 20260925-x",
                    "vnx dispatch stage --instruction i.md --dispatch-id x --role r --slot T1",
                    "vnx dispatch-agent --role backend-developer",
                    "gh pr create --title t --body b",
                    "python3 scripts/lib/dispatch_bridge.py stage --dispatch-id x",
                    "python3 -c 'from dispatch_bridge import stage_spec_bundle; stage_spec_bundle(x)'"):
        decision = _bash_pre(tmp_path, env_base, 500_000, command)
        assert _denied(decision), command
        assert "rotate" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_force_lets_finishing_work_through(tmp_path, env_base):
    for command in ("vnx dispatch --dry-run 20260925-x",
                    "vnx dispatch 20260925-x --dry-run",
                    "python3 scripts/pr_merge.py --dispatch-id 20260925-x 1920",
                    "gh pr checks 1920 --watch",
                    "gh pr view 1920",
                    "grep -n stage_spec_bundle scripts/lib/dispatch_bridge.py",
                    "cat scripts/lib/dispatch_bridge.py",
                    "bash scripts/t0_rotate_spawn.sh",
                    "tmux kill-window -t @3"):
        assert _bash_pre(tmp_path, env_base, 550_000, command) is None, command
    assert _run(tmp_path, env_base, "PreToolUse", 550_000, tool_name="Read",
                tool_input={"file_path": "/x"}) is None


def test_force_blocks_a_new_goal(tmp_path, env_base):
    assert _denied(_run(tmp_path, env_base, "PreToolUse", 520_000, tool_name="Skill",
                        tool_input={"skill": "goal", "args": "iets nieuws"}))
    assert _denied(_run(tmp_path, env_base, "PreToolUse", 520_000, tool_name="SlashCommand",
                        tool_input={"command": "/goal iets nieuws"}))
    decision = _run(tmp_path, env_base, "UserPromptSubmit", 520_000, prompt="/goal iets nieuws")
    assert decision["decision"] == "block"
    assert "/goal" in decision["reason"]
    # other skills (the rotation itself) pass
    assert _run(tmp_path, env_base, "PreToolUse", 520_000, tool_name="Skill",
                tool_input={"skill": "rotate"}) is None


def test_force_prompt_gets_force_context(tmp_path, env_base):
    decision = _run(tmp_path, env_base, "UserPromptSubmit", 510_000, prompt="status?")
    assert "start niets nieuws" in decision["hookSpecificOutput"]["additionalContext"]


def test_force_stop_blocks_once_per_turn(tmp_path, env_base):
    decision = _run(tmp_path, env_base, "Stop", 500_000, stop_hook_active=False)
    assert decision["decision"] == "block"
    assert "Rond af wat nog loopt" in decision["reason"]
    assert "rotate-skill" in decision["reason"]
    # the stop that follows the continuation passes
    assert _run(tmp_path, env_base, "Stop", 500_000, stop_hook_active=True) is None
    # the next turn is blocked again
    assert _run(tmp_path, env_base, "Stop", 500_000, stop_hook_active=False)["decision"] == "block"


def test_force_stop_respects_the_session_latch(tmp_path, env_base):
    sdir = Path(env_base["VNX_T0_ROTATION_STATE_DIR"])
    rotation_state.write_latch(sdir, session_id=SESSION, pane="", old_window="@1", new_window="@2")
    assert _run(tmp_path, env_base, "Stop", 500_000) is None
    # the latch releases the stop only; starting new work stays refused
    assert _denied(_bash_pre(tmp_path, env_base, 500_000, "gh pr create"))


def test_force_stop_respects_the_pane_latch(tmp_path, env_base):
    sdir = Path(env_base["VNX_T0_ROTATION_STATE_DIR"])
    rotation_state.write_latch(sdir, session_id=None, pane=PANE, old_window="@1", new_window="@2")
    assert _run(tmp_path, env_base, "Stop", 500_000) is None


def test_expired_latch_rearms_the_stop(tmp_path, env_base):
    sdir = Path(env_base["VNX_T0_ROTATION_STATE_DIR"])
    [latch] = rotation_state.write_latch(sdir, session_id=SESSION, pane="", old_window="@1",
                                         new_window="@2")
    old = time.time() - rotation_state.LATCH_TTL_SECONDS - 60
    os.utime(latch, (old, old))
    assert _run(tmp_path, env_base, "Stop", 500_000)["decision"] == "block"


def test_hard_stop_demands_rotation_now(tmp_path, env_base):
    decision = _run(tmp_path, env_base, "Stop", 600_000)
    assert decision["decision"] == "block"
    assert "NU" in decision["reason"]
    assert "ook als er nog werk loopt" in decision["reason"]


def test_force_writes_pending_marker_and_one_receipt_per_transition(tmp_path, env_base):
    _run(tmp_path, env_base, "Stop", 500_000)
    _run(tmp_path, env_base, "Stop", 505_000)
    sdir = Path(env_base["VNX_T0_ROTATION_STATE_DIR"])
    assert rotation_state.pending_session_id(sdir, PANE) == SESSION
    receipts = _receipts(env_base)
    assert [r["trigger"] for r in receipts] == ["t0_context_rotation_pending"]
    assert receipts[0]["receipt_kind"] == "state_mutation"
    assert receipts[0]["level"] == "force"
    _run(tmp_path, env_base, "Stop", 600_000)
    assert [r["level"] for r in _receipts(env_base)] == ["force", "hard"]


# ── worker sessions are always a no-op ──────────────────────────────────────


def _worker_cases(tmp_path, env_base):
    no_terminal = {k: v for k, v in env_base.items() if k != "VNX_TERMINAL"}
    yield "dispatch-id", {**no_terminal, "VNX_DISPATCH_ID": "20260925-x"}, None
    yield "terminal T2", {**env_base, "VNX_TERMINAL": "T2"}, None
    yield "cwd terminals/T1", no_terminal, tmp_path / "proj" / ".claude" / "terminals" / "T1"
    yield "dispatch worktree", no_terminal, tmp_path / "proj" / ".vnx-data" / "worktrees" / "dispatch-x"
    yield "dispatch worktree with T0 env", env_base, tmp_path / "p2" / ".vnx-data" / "worktrees" / "dispatch-y"


def test_worker_sessions_are_a_noop_in_every_band(tmp_path, env_base):
    for label, env, cwd in _worker_cases(tmp_path, env_base):
        for event in ("UserPromptSubmit", "PreToolUse", "Stop"):
            decision = _run(tmp_path, env, event, 900_000, cwd=cwd, prompt="/goal x",
                            tool_name="Bash", tool_input={"command": "vnx dispatch x"})
            assert decision is None, (label, event, decision)
    assert _receipts(env_base) == []
    assert not Path(env_base["VNX_T0_ROTATION_STATE_DIR"]).exists()


def test_t0_without_explicit_terminal_is_guarded_at_project_root(tmp_path, env_base):
    env = {k: v for k, v in env_base.items() if k != "VNX_TERMINAL"}
    assert _run(tmp_path, env, "Stop", 500_000)["decision"] == "block"


def test_garbage_stdin_is_silent(tmp_path, env_base):
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json", env=env_base,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout == ""


# ── registration ─────────────────────────────────────────────────────────────


def _guard_events(settings: dict) -> set:
    events = set()
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if "t0_context_guard.py" in hook.get("command", ""):
                    events.add(event)
                    if event == "PreToolUse":
                        assert set(group["matcher"].split("|")) >= {"Bash", "Skill"}
    return events


def test_guard_registered_in_fabric_settings():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    assert _guard_events(settings) == {"UserPromptSubmit", "PreToolUse", "Stop"}


def test_guard_registered_in_consumer_template(tmp_path):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import vnx_settings_merge

    settings = vnx_settings_merge.load_template(str(REPO_ROOT), str(tmp_path / "project"))
    assert _guard_events(settings) == {"UserPromptSubmit", "PreToolUse", "Stop"}


@pytest.mark.parametrize("variant", ["default", "minimal"])
def test_guard_registered_in_init_templates(variant):
    from jinja2 import Environment, FileSystemLoader

    path = REPO_ROOT / "templates" / "init" / variant
    rendered = Environment(loader=FileSystemLoader(str(path))).get_template("settings.json.j2").render(
        project_name="p", project_id="p", vnx_version="0", engine_root=str(REPO_ROOT))
    assert _guard_events(json.loads(rendered)) == {"UserPromptSubmit", "PreToolUse", "Stop"}
