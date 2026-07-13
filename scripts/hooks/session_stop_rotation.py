#!/usr/bin/env python3
"""Stop hook safety-net for T0 context rotation (DEFAULT OFF).

NO-OP unless VNX_T0_ROTATION=1. When enabled, it ONLY ensures handoff.md
exists at the project_id+terminal-scoped rotation dir — a safety net for the
case where T0 goes idle (Stop fires) without ever calling
scripts/lib/context_rotation.checkpoint() itself (e.g. no governance boundary
was reached, or rotation was disabled mid-session). It makes NO claim to
spawn a successor T0 — the respawn is strictly T0-INITIATED
(context_rotation.checkpoint() -> respawn()), never hook-initiated (rev-3
plan §"Rev-3 decision", point 3).

The Stop hook's settings.json matcher is empty ("") — it fires for EVERY
session, T0 or worker. Since the handoff it writes is always the T0 one,
this module verifies it IS a T0 session (env identity, falling back to the
same cwd-based .claude/terminals/T{1,2,3} heuristic stop_report_hook.sh
already uses to detect workers) before writing anything, so a stopping
T1/T2/T3 can never clobber the T0 rotation handoff with worker state.

Claude Code Stop hook contract: JSON on stdin ({session_id, transcript_path,
cwd, ...}), JSON decision on stdout, exit 0 always (never block session
stop).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Matches .claude/terminals/T1, T2, or T3 anywhere in a cwd — mirrors the
# case statement in scripts/hooks/stop_report_hook.sh's cwd-based terminal
# detection. T0 has no such segment (it runs at the project root), so its
# absence is the T0 signal.
_WORKER_TERMINAL_CWD_RE = re.compile(r"(?:^|/)\.claude/terminals/(T[1-3])(?:/|$)")


def _is_t0_session(cwd: str, env: dict) -> bool:
    """Best-effort, in-script T0 identity check (portable — does not rely on
    harness-level hook-matcher scoping). An explicit VNX_TERMINAL /
    VNX_TERMINAL_ID env var wins when set; otherwise falls back to the cwd
    heuristic. No T1/T2/T3 signal at all -> assume T0 (matches the existing
    default: T0 runs at the project root, not under terminals/T{n}).

    OI-619 finding #2: a tmux-spawn dispatch worker inherits VNX_T0_ROTATION
    from the parent T0 environment but is NOT itself T0 — it typically has no
    VNX_TERMINAL set AND runs in an isolated dispatch worktree
    (.vnx-data/worktrees/dispatch-*), which does not match the
    .claude/terminals/T{1,2,3} cwd pattern either. Without a check, such a
    worker fell through to the "assume T0" default and could overwrite the
    real T0's handoff on Stop. VNX_DISPATCH_ID / VNX_TMUX_SIGNAL_DIR are the
    repo-wide markers a dispatched worker always carries (set together by
    tmux_interactive_dispatch.py before `claude` launches — see
    hooks/git/pre-push and scripts/hooks/tmux_signal_*.sh for the same
    convention) — their presence means NON-T0 regardless of cwd, unless
    VNX_TERMINAL was explicitly set to T0 above.
    """
    terminal_env = env.get("VNX_TERMINAL") or env.get("VNX_TERMINAL_ID")
    if terminal_env:
        return terminal_env.strip().upper() == "T0"
    if env.get("VNX_DISPATCH_ID") or env.get("VNX_TMUX_SIGNAL_DIR"):
        return False
    return _WORKER_TERMINAL_CWD_RE.search((cwd or "").replace("\\", "/")) is None


def main() -> int:
    if os.environ.get("VNX_T0_ROTATION") != "1":
        sys.stdout.write("{}\n")
        return 0

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()

    if not _is_t0_session(cwd, os.environ):
        sys.stdout.write("{}\n")
        return 0

    project_root = Path(cwd)

    try:
        from project_root import resolve_project_id
        from context_rotation import write_t0_handoff, rotation_handoff_dir, DEFAULT_TERMINAL

        project_id = resolve_project_id(str(project_root))
        logdir = rotation_handoff_dir(project_id, DEFAULT_TERMINAL)
        write_t0_handoff(logdir=logdir, project_root=project_root, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - safety net must never block session stop
        sys.stderr.write(f"session_stop_rotation: safety-net write failed: {exc}\n")

    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
