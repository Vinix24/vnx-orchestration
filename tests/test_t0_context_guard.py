"""tests/test_t0_context_guard.py — the T0 context guard hook (dispatch 20260925-t0-context-rotation-enforced).

Every test runs the real hook script as a subprocess with a hook payload on stdin and a
synthetic transcript, in an environment built from scratch: the test process itself may run
inside a dispatch (VNX_DISPATCH_ID set), which would make every call a worker no-op.
Rotation state and receipts go to tmp paths via VNX_T0_ROTATION_STATE_DIR and
VNX_T0_ROTATION_RECEIPTS_FILE, never to a real store.

The project a payload's cwd lives in is a git repository with a ``.vnx-project-id`` and a
``.claude/terminals/T0/CLAUDE.md`` (rule 1 of plan t0-guard-gebruikersniveau), unless a test is
about a project without one of them.
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

import hook_coverage
import t0_rotation_state as rotation_state

SESSION = "sess-abc-123"
PANE = "%42"

# Hook environments measured on 2026-10-16 (dispatch 20261016b-tg-g1-shim-coverage report).
MEASURED_T0_ENV = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1",
                   "CLAUDE_CODE_CHILD_SESSION": "1"}
MEASURED_CLAUDE_P_ENV = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_CODE_SESSION_ATTENDED": "0",
                         "CLAUDE_CODE_CHILD_SESSION": "1"}


def _vnx_project(path: Path, project_id: Optional[str] = "consumer", *, t0: bool = True,
                 git: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if git and not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HOME": str(path.parent), "GIT_CONFIG_NOSYSTEM": "1"})
    if project_id is not None:
        (path / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    if t0:
        (path / ".claude" / "terminals" / "T0").mkdir(parents=True, exist_ok=True)
        (path / ".claude" / "terminals" / "T0" / "CLAUDE.md").write_text("# T0\n", encoding="utf-8")
    return path


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
    project = cwd or _vnx_project(tmp_path / "project")
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


def test_a_repeat_in_the_same_band_does_not_touch_the_marker(tmp_path, env_base):
    _run(tmp_path, env_base, "Stop", 500_000)
    marker = rotation_state.pending_path(Path(env_base["VNX_T0_ROTATION_STATE_DIR"]), PANE)
    before = marker.read_bytes()
    os.utime(marker, (1_000_000_000, 1_000_000_000))

    _run(tmp_path, env_base, "PreToolUse", 530_000, tool_name="Bash", tool_input={"command": "ls"})
    _run(tmp_path, env_base, "Stop", 560_000)

    assert marker.read_bytes() == before
    assert marker.stat().st_mtime == 1_000_000_000
    assert len(_receipts(env_base)) == 1


def test_a_refusing_ledger_keeps_the_guard_working_and_the_transition_retries(tmp_path, env_base):
    """Ledger first (ADR-005): with no receipt there is no marker, and the guard still blocks.
    Once the ledger takes the append, the same crossing is recorded exactly once."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the receipts directory should be", encoding="utf-8")
    broken = {**env_base, "VNX_T0_ROTATION_RECEIPTS_FILE": str(blocker / "t0_receipts.ndjson")}
    marker = rotation_state.pending_path(Path(env_base["VNX_T0_ROTATION_STATE_DIR"]), PANE)

    assert _run(tmp_path, broken, "Stop", 500_000)["decision"] == "block"
    assert not marker.exists()

    assert _run(tmp_path, env_base, "Stop", 505_000)["decision"] == "block"
    assert marker.exists()
    assert [r["trigger"] for r in _receipts(env_base)] == ["t0_context_rotation_pending"]


# ── worker sessions are always a no-op ──────────────────────────────────────


def _worker_cases(tmp_path, env_base):
    """Every case lives inside a project that passes rule 1, so the rule that refuses is the
    one the case names."""
    no_terminal = {k: v for k, v in env_base.items() if k != "VNX_TERMINAL"}
    proj = _vnx_project(tmp_path / "proj")
    p2 = _vnx_project(tmp_path / "p2")
    yield "dispatch-id", {**no_terminal, "VNX_DISPATCH_ID": "20260925-x"}, None
    yield "terminal T2", {**env_base, "VNX_TERMINAL": "T2"}, None
    yield "cwd terminals/T1", no_terminal, proj / ".claude" / "terminals" / "T1"
    yield "dispatch worktree", no_terminal, proj / ".vnx-data" / "worktrees" / "dispatch-x"
    yield "dispatch worktree with T0 env", env_base, p2 / ".vnx-data" / "worktrees" / "dispatch-y"
    yield "claude -p child of the T0", {**env_base, **MEASURED_CLAUDE_P_ENV}, None
    yield "worker-scoped seat", {**no_terminal, **MEASURED_T0_ENV, "VNX_WORKER_SCOPED": "1"}, None


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


# ── who is guarded: rules 1-3 ───────────────────────────────────────────────


def _no_terminal(env_base) -> Dict[str, str]:
    return {k: v for k, v in env_base.items() if k != "VNX_TERMINAL"}


def _all_events_silent(tmp_path, env, cwd, tokens=520_000) -> bool:
    return all(_run(tmp_path, env, event, tokens, cwd=cwd, prompt="/goal x", tool_name="Bash",
                    tool_input={"command": "vnx dispatch x"}) is None
               for event in ("UserPromptSubmit", "PreToolUse", "Stop"))


def test_the_measured_interactive_t0_is_guarded(tmp_path, env_base):
    env = {**_no_terminal(env_base), **MEASURED_T0_ENV}
    assert _run(tmp_path, env, "Stop", 520_000)["decision"] == "block"


def test_the_t0_folder_is_guarded(tmp_path, env_base):
    env = {**_no_terminal(env_base), **MEASURED_T0_ENV}
    t0_dir = _vnx_project(tmp_path / "project") / ".claude" / "terminals" / "T0"
    assert _run(tmp_path, env, "Stop", 520_000, cwd=t0_dir)["decision"] == "block"


def test_an_sdk_cli_session_gets_no_decision(tmp_path, env_base):
    """Rule 3: a `claude -p` the T0 starts inherits its pane and, here, an explicit T0 terminal."""
    env = {**env_base, **MEASURED_CLAUDE_P_ENV}
    assert _all_events_silent(tmp_path, env, _vnx_project(tmp_path / "project"))
    assert _receipts(env_base) == []


@pytest.mark.parametrize("entrypoint", ["sdk-ts", "sdk-py"])
def test_an_agent_sdk_session_gets_no_decision(tmp_path, env_base, entrypoint):
    env = {**env_base, "CLAUDE_CODE_ENTRYPOINT": entrypoint}
    assert _all_events_silent(tmp_path, env, _vnx_project(tmp_path / "project"))


def test_a_worker_scoped_session_gets_no_decision(tmp_path, env_base):
    env = {**_no_terminal(env_base), **MEASURED_T0_ENV, "VNX_WORKER_SCOPED": "1"}
    assert _all_events_silent(tmp_path, env, _vnx_project(tmp_path / "project"))
    assert _receipts(env_base) == []


def test_worker_scoped_zero_does_not_unguard(tmp_path, env_base):
    env = {**_no_terminal(env_base), **MEASURED_T0_ENV, "VNX_WORKER_SCOPED": "0"}
    assert _run(tmp_path, env, "Stop", 520_000)["decision"] == "block"


def test_a_project_id_without_a_t0_folder_gets_no_decision(tmp_path, env_base):
    """Rule 1: `~/Desktop/BUSINESS/clients` carries an id (`vincent-clients`) and no T0 folder."""
    clients = _vnx_project(tmp_path / "clients", "vincent-clients", t0=False)
    assert _all_events_silent(tmp_path, env_base, clients)
    assert _receipts(env_base) == []


@pytest.mark.parametrize("content", [None, "", "\nvnx-dev\n", "  \t\n"])
def test_a_t0_folder_without_a_project_id_gets_no_decision(tmp_path, env_base, content):
    project = _vnx_project(tmp_path / "project", None)
    if content is not None:
        (project / ".vnx-project-id").write_text(content, encoding="utf-8")
    assert _all_events_silent(tmp_path, env_base, project)


def test_outside_a_git_repository_gets_no_decision(tmp_path, env_base):
    assert _all_events_silent(tmp_path, env_base, _vnx_project(tmp_path / "plain", git=False))


def test_a_side_session_opts_out_with_vnx_terminal(tmp_path, env_base):
    env = {**env_base, **MEASURED_T0_ENV, "VNX_TERMINAL": "notes"}
    assert _all_events_silent(tmp_path, env, _vnx_project(tmp_path / "project"))


# ── user scope: the project's own registration wins ─────────────────────────


def _direct(path: Path) -> dict:
    return {"type": "command", "command": f'python3 "{path}"'}


def _covering_project(tmp_path: Path, hooks: dict, name: str = "settings.json") -> Path:
    project = _vnx_project(tmp_path / "project")
    (project / ".claude" / name).write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return project


def _user_env(env_base, project: Path, scope: Optional[str] = "user") -> Dict[str, str]:
    env = {**env_base, "CLAUDE_PROJECT_DIR": str(project)}
    if scope is not None:
        env["VNX_HOOK_SCOPE"] = scope
    return env


def test_user_scope_skips_an_event_the_project_covers(tmp_path, env_base):
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(HOOK)]}]})
    env = _user_env(env_base, project)
    assert _run(tmp_path, env, "Stop", 520_000) is None
    # the project does not cover UserPromptSubmit, so the user-level call still decides
    assert _run(tmp_path, env, "UserPromptSubmit", 520_000, prompt="status") is not None


@pytest.mark.parametrize("scope", ["project", None])
def test_a_project_scope_call_never_skips(tmp_path, env_base, scope):
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(HOOK)]}]})
    assert _run(tmp_path, _user_env(env_base, project, scope), "Stop", 520_000)["decision"] == "block"


def test_user_scope_follows_the_pretooluse_matcher(tmp_path, env_base):
    project = _covering_project(tmp_path, {"PreToolUse": [{"matcher": "Bash",
                                                            "hooks": [_direct(HOOK)]}]})
    env = _user_env(env_base, project)
    assert _bash_pre(tmp_path, env, 520_000, "vnx dispatch 20261016-x") is None
    assert _denied(_run(tmp_path, env, "PreToolUse", 520_000, tool_name="Skill",
                        tool_input={"skill": "goal"}))


def test_user_scope_runs_when_the_project_entry_points_at_a_missing_file(tmp_path, env_base):
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(tmp_path / "gone.py")]}]})
    assert _run(tmp_path, _user_env(env_base, project), "Stop", 520_000)["decision"] == "block"


def _shim_hook(vnx_home: Path) -> dict:
    return {"type": "command", "command": hook_coverage.registered_command(
        hook_coverage.GUARD_ENTRY, "project", vnx_home=str(vnx_home))}


def test_user_scope_runs_when_the_project_shim_is_missing(tmp_path, env_base):
    """codex advisory on #2075: a project registration through a shim that is gone exits 1
    itself, so the user-level call must decide instead of skipping."""
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_shim_hook(tmp_path / "pruned")]}]})
    assert _run(tmp_path, _user_env(env_base, project), "Stop", 520_000)["decision"] == "block"


def test_user_scope_skips_where_the_project_shim_exists(tmp_path, env_base):
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_shim_hook(REPO_ROOT)]}]})
    assert _run(tmp_path, _user_env(env_base, project), "Stop", 520_000) is None


def test_user_scope_runs_when_the_project_settings_are_invalid(tmp_path, env_base):
    project = _vnx_project(tmp_path / "project")
    (project / ".claude" / "settings.json").write_text('{"hooks": {"Stop": [', encoding="utf-8")
    (project / ".claude" / "settings.local.json").mkdir()
    assert _run(tmp_path, _user_env(env_base, project), "Stop", 520_000)["decision"] == "block"


def test_user_scope_counts_settings_local(tmp_path, env_base):
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(HOOK)]}]},
                                name="settings.local.json")
    assert _run(tmp_path, _user_env(env_base, project), "Stop", 520_000) is None


def test_user_scope_reads_the_settings_claude_code_loaded(tmp_path, env_base):
    """CLAUDE_PROJECT_DIR, not the git toplevel: a session started in a subdirectory has no
    project hooks, so the user-level guard is the one that runs."""
    project = _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(HOOK)]}]})
    sub = project / "docs"
    sub.mkdir()
    env = _user_env(env_base, sub)
    assert _run(tmp_path, env, "Stop", 520_000, cwd=sub)["decision"] == "block"
    env_root = _user_env(env_base, project)
    assert _run(tmp_path, env_root, "Stop", 520_000, cwd=sub) is None


def test_user_scope_without_claude_project_dir_runs(tmp_path, env_base):
    _covering_project(tmp_path, {"Stop": [{"hooks": [_direct(HOOK)]}]})
    env = {**env_base, "VNX_HOOK_SCOPE": "user"}
    assert _run(tmp_path, env, "Stop", 520_000)["decision"] == "block"


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
