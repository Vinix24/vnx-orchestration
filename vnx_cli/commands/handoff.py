#!/usr/bin/env python3
"""vnx handoff — repo-level reader for the T0 context-rotation handoff.

Two verbs:
  vnx handoff show [--logdir DIR] [--terminal T0] [--mark-ready --rotation-id ID]
      Prints the resume briefing parsed from <logdir>/handoff.md. This is
      what a freshly-respawned T0 runs first (scripts/lib/context_rotation.
      respawn() preloads exactly this instruction into the successor's tmux
      pane) — NOT the personal /kickoff skill; this is the repo-level
      contract (docs/operations/CONTEXT_ROTATION.md).
  vnx handoff mark-ready --rotation-id ID [--terminal T0]
      Writes the rotation_id-stamped `.ready` signal the old T0's respawn()
      call is waiting on (round-3 finding #6: the waiter validates the
      rotation_id, so a stale `.ready` from a previous rotation can never
      false-confirm a new one).

--logdir defaults to the SAME project_id+terminal-scoped path
context_rotation.checkpoint() writes to, so `vnx handoff show` with no flags
finds the handoff a real rotation produced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from vnx_cli import _engine


def _resolve_project_id(args: Any) -> str:
    pid = getattr(args, "project_id", None)
    if pid:
        return pid
    _engine.ensure_engine_on_path()
    from project_root import resolve_project_id as _resolve
    try:
        return _resolve(getattr(args, "project_dir", "."))
    except RuntimeError as exc:
        print(f"Error: --project-id not supplied and auto-resolution failed: {exc}", file=sys.stderr)
        sys.exit(2)


def _resolve_logdir(args: Any, project_id: str) -> Path:
    logdir = getattr(args, "logdir", None)
    if logdir:
        return Path(logdir)
    _engine.ensure_engine_on_path()
    from context_rotation import rotation_handoff_dir
    terminal = getattr(args, "terminal", "T0") or "T0"
    return rotation_handoff_dir(project_id, terminal)


def _mark_ready(args: Any, project_id: str) -> int:
    rotation_id = getattr(args, "rotation_id", None)
    if not rotation_id:
        print("Error: --rotation-id is required to mark ready", file=sys.stderr)
        return 2
    terminal = getattr(args, "terminal", "T0") or "T0"

    _engine.ensure_engine_on_path()
    from context_rotation import write_ready_signal

    ready_path = write_ready_signal(project_id, terminal, rotation_id)
    print(f"ready: terminal={terminal} rotation_id={rotation_id} -> {ready_path}")
    return 0


def _show(args: Any) -> int:
    project_id = _resolve_project_id(args)
    logdir = _resolve_logdir(args, project_id)

    _engine.ensure_engine_on_path()
    from handoff_reader import read_handoff, format_briefing

    handoff_path = logdir / "handoff.md"
    briefing = read_handoff(handoff_path)
    if briefing is None:
        print(f"No handoff found at {handoff_path}", file=sys.stderr)
        return 1

    print(format_briefing(briefing))

    if getattr(args, "mark_ready", False):
        return _mark_ready(args, project_id)
    return 0


def vnx_handoff(args: Any) -> int:
    sub = getattr(args, "handoff_subcommand", None)
    if sub in (None, "show"):
        return _show(args)
    if sub == "mark-ready":
        project_id = _resolve_project_id(args)
        return _mark_ready(args, project_id)
    print(f"Unknown handoff subcommand: {sub}", file=sys.stderr)
    return 2
