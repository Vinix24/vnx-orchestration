#!/usr/bin/env python3
"""t0_lifecycle_cli.py — Operator CLI for per-project T0 lifecycle management.

Wave 5 PR-5.2. Wraps T0LifecycleManager with an argparse interface.

Usage:
    python3 scripts/t0_lifecycle_cli.py spawn --project-id vnx-dev --project-root /path/to/repo
    python3 scripts/t0_lifecycle_cli.py heartbeat --project-id vnx-dev --pid 12345
    python3 scripts/t0_lifecycle_cli.py list
    python3 scripts/t0_lifecycle_cli.py kill --project-id vnx-dev
    python3 scripts/t0_lifecycle_cli.py reap

Config resolution (in priority order):
    1. --coord-db CLI flag
    2. VNX_STATE_DIR env var  → <VNX_STATE_DIR>/runtime_coordination.db
    3. Default: .vnx-data/state/runtime_coordination.db (relative to cwd)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

# Allow running from project root or scripts/ directory
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.aggregator.t0_lifecycle import T0LifecycleManager


def _resolve_coord_db(args: argparse.Namespace) -> Path:
    if getattr(args, "coord_db", None):
        return Path(args.coord_db)
    state_dir = os.environ.get("VNX_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "runtime_coordination.db"
    return Path.cwd() / ".vnx-data" / "state" / "runtime_coordination.db"


def _build_manager(
    coord_db: Path,
    project_id: str = "",
    project_root: str = "",
    claude_args: list | None = None,
) -> T0LifecycleManager:
    projects: dict = {}
    if project_id:
        projects[project_id] = {
            "root": project_root,
            "claude_args": claude_args or [],
        }
    return T0LifecycleManager({
        "coord_db_path": str(coord_db),
        "projects": projects,
    })


def cmd_spawn(args: argparse.Namespace) -> int:
    coord_db = _resolve_coord_db(args)
    claude_args = args.claude_args.split() if args.claude_args else []
    mgr = _build_manager(
        coord_db,
        project_id=args.project_id,
        project_root=args.project_root or "",
        claude_args=claude_args,
    )
    try:
        instance = mgr.spawn(args.project_id)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "project_id": instance.project_id,
        "pid": instance.pid,
        "state": instance.state,
        "started_at": instance.started_at,
        "project_root": instance.project_root,
    }, indent=2))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    coord_db = _resolve_coord_db(args)
    mgr = _build_manager(coord_db, project_id=args.project_id)
    recorded = mgr.heartbeat(args.project_id, args.pid)
    if recorded:
        print(f"[ok] heartbeat recorded for project={args.project_id} pid={args.pid}")
        return 0
    print(f"[warn] no running T0 found for project={args.project_id} pid={args.pid}", file=sys.stderr)
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    coord_db = _resolve_coord_db(args)
    mgr = T0LifecycleManager({"coord_db_path": str(coord_db)})
    instances = mgr.list_running()
    if not instances:
        print("No running T0 instances.")
        return 0
    for inst in instances:
        print(json.dumps({
            "project_id": inst.project_id,
            "pid": inst.pid,
            "state": inst.state,
            "started_at": inst.started_at,
            "last_heartbeat_at": inst.last_heartbeat_at,
        }))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    coord_db = _resolve_coord_db(args)
    mgr = _build_manager(coord_db, project_id=args.project_id)
    sig = signal.SIGKILL if args.force else signal.SIGTERM
    killed = mgr.kill(args.project_id, signal_type=sig)
    if killed:
        sig_name = "SIGKILL" if args.force else "SIGTERM"
        print(f"[ok] sent {sig_name} to T0 for project={args.project_id}")
        return 0
    print(f"[warn] no running T0 found for project={args.project_id}", file=sys.stderr)
    return 1


def cmd_reap(args: argparse.Namespace) -> int:
    coord_db = _resolve_coord_db(args)
    mgr = T0LifecycleManager({"coord_db_path": str(coord_db)})
    reaped = mgr.reap_dead_t0s()
    if reaped:
        print(f"[ok] reaped {len(reaped)} stale T0(s): {', '.join(reaped)}")
    else:
        print("[ok] no stale T0s found")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VNX per-project T0 lifecycle management (Wave 5 PR-5.2)"
    )
    parser.add_argument(
        "--coord-db",
        default=None,
        help="Path to runtime_coordination.db (overrides VNX_STATE_DIR)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sp_spawn = sub.add_parser("spawn", help="Spawn a per-project T0 process")
    sp_spawn.add_argument("--project-id", required=True)
    sp_spawn.add_argument("--project-root", default="", help="Absolute path to project worktree")
    sp_spawn.add_argument("--claude-args", default="", help="Space-separated extra args for claude")
    sp_spawn.set_defaults(func=cmd_spawn)

    sp_hb = sub.add_parser("heartbeat", help="Record heartbeat for a running T0")
    sp_hb.add_argument("--project-id", required=True)
    sp_hb.add_argument("--pid", required=True, type=int)
    sp_hb.set_defaults(func=cmd_heartbeat)

    sp_list = sub.add_parser("list", help="List all running T0 instances")
    sp_list.set_defaults(func=cmd_list)

    sp_kill = sub.add_parser("kill", help="Kill a running T0 (graceful SIGTERM, then SIGKILL)")
    sp_kill.add_argument("--project-id", required=True)
    sp_kill.add_argument("--force", action="store_true", help="Skip SIGTERM, send SIGKILL directly")
    sp_kill.set_defaults(func=cmd_kill)

    sp_reap = sub.add_parser("reap", help="Reap stale T0s (heartbeat older than timeout)")
    sp_reap.set_defaults(func=cmd_reap)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
