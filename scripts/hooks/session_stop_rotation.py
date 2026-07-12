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

Claude Code Stop hook contract: JSON on stdin ({session_id, transcript_path,
cwd, ...}), JSON decision on stdout, exit 0 always (never block session
stop).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


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
