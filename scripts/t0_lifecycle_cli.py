#!/usr/bin/env python3
"""t0_lifecycle_cli.py — Wave 5 PR-5.2: operator CLI for T0 lifecycle.

Wraps T0LifecycleManager (scripts/aggregator/t0_lifecycle.py). Aggregator is
constructed and injected — never None. Commands:

    spawn         Spawn a new T0 worker for a project_id.
    heartbeat     Record a heartbeat for an active T0 (token required).
    kill          Terminate a specific T0 incarnation (token required).
    force-release Operator escape: release a lease without process check.
    list          Show all running T0 leases.
    reap          Run the stale-heartbeat reap loop once.

Output: human-readable text for `list`, JSON for everything else.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from aggregator.state_aggregator import StateAggregator
from aggregator.t0_lifecycle import (
    LeaseTokenMismatchError,
    T0AlreadyRunningError,
    T0AuditEmitError,
    T0LifecycleManager,
    T0SpawnContentionError,
    T0SpawnFailedError,
    T0SubprocessExitedEarly,
)


def _canonical_paths() -> dict | None:
    """Resolve VNX_STATE_DIR/VNX_DATA_DIR via canonical vnx_paths (central-mode aware)."""
    try:
        lib_dir = str(_SCRIPTS_DIR / "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from vnx_paths import resolve_paths
        return resolve_paths()
    except Exception:
        return None


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    canonical = _canonical_paths()

    if args.coord_db:
        coord_db = Path(args.coord_db).resolve()
    else:
        state_dir = os.environ.get("VNX_STATE_DIR")
        if not state_dir:
            # A bare ".vnx-data/state" default resolves relative to CWD, ignoring
            # the central ~/.vnx-data/<project>/state location. See #1023.
            state_dir = canonical["VNX_STATE_DIR"] if canonical else ".vnx-data/state"
        coord_db = Path(state_dir).resolve() / "runtime_coordination.db"

    if args.vnx_data_dir:
        vnx_data = Path(args.vnx_data_dir).resolve()
    else:
        vnx_data_env = os.environ.get("VNX_DATA_DIR")
        if not vnx_data_env:
            vnx_data_env = canonical["VNX_DATA_DIR"] if canonical else ".vnx-data"
        vnx_data = Path(vnx_data_env).resolve()

    return coord_db, vnx_data


def _build_manager(args: argparse.Namespace) -> T0LifecycleManager:
    coord_db, vnx_data = _resolve_paths(args)
    if not coord_db.exists():
        print(
            json.dumps(
                {"ok": False, "error": f"coord_db not found: {coord_db}"},
                indent=2,
            )
        )
        sys.exit(2)
    aggregator = StateAggregator(vnx_data)
    return T0LifecycleManager(coord_db, aggregator)


def _cmd_spawn(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    argv = None
    if args.argv:
        argv = args.argv
    try:
        instance = mgr.spawn(
            args.project_id,
            argv=argv,
            project_root=args.project_root,
        )
    except T0AlreadyRunningError as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "already_running"}, indent=2))
        return 3
    except T0SubprocessExitedEarly as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "exited_early"}, indent=2))
        return 4
    except T0SpawnFailedError as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "spawn_failed"}, indent=2))
        return 5
    except T0SpawnContentionError as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "db_contention"}, indent=2))
        return 6
    except T0AuditEmitError as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "audit_emit_failed"}, indent=2))
        return 7

    print(json.dumps({"ok": True, "instance": asdict(instance)}, indent=2))
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    ok = mgr.heartbeat(args.project_id, args.pid, args.lease_token)
    print(json.dumps({"ok": ok}, indent=2))
    return 0 if ok else 1


def _cmd_kill(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    sig = signal.SIGKILL if args.sigkill else signal.SIGTERM
    try:
        kr = mgr.kill(
            args.project_id,
            args.lease_token,
            signal_type=sig,
            wait_timeout=args.wait,
        )
    except LeaseTokenMismatchError as e:
        print(json.dumps({"ok": False, "error": str(e), "kind": "token_mismatch"}, indent=2))
        return 8

    print(json.dumps({"ok": kr.verified_dead, "result": asdict(kr)}, indent=2))
    return 0 if kr.verified_dead else 1


def _cmd_force_release(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    released = mgr.force_release_lease(args.lease_token, args.reason)
    print(json.dumps({"ok": released}, indent=2))
    return 0 if released else 1


def _cmd_list(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    instances = mgr.list_running()
    if args.json:
        print(json.dumps([asdict(i) for i in instances], indent=2))
        return 0
    if not instances:
        print("(no running T0 leases)")
        return 0
    print(f"{'PROJECT':<24} {'PID':>8} {'GEN':>4} {'STATE':<14} TOKEN")
    for inst in instances:
        print(
            f"{inst.project_id:<24} {inst.pid:>8} {inst.generation:>4} "
            f"{inst.lifecycle_state:<14} {inst.lease_token[:16]}..."
        )
    return 0


def _cmd_reap(args: argparse.Namespace) -> int:
    mgr = _build_manager(args)
    results = mgr.reap_dead_t0s()
    print(json.dumps([asdict(r) for r in results], indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Operator CLI for T0 lifecycle management"
    )
    p.add_argument("--coord-db", help="Path to runtime_coordination.db (default: $VNX_STATE_DIR/runtime_coordination.db)")
    p.add_argument("--vnx-data-dir", help="Path to .vnx-data (default: $VNX_DATA_DIR)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_spawn = sub.add_parser("spawn", help="Spawn a new T0 worker")
    sp_spawn.add_argument("project_id")
    sp_spawn.add_argument("--project-root", default=None)
    sp_spawn.add_argument(
        "--argv", nargs=argparse.REMAINDER,
        help="Subprocess argv (after --)",
    )
    sp_spawn.set_defaults(func=_cmd_spawn)

    sp_hb = sub.add_parser("heartbeat", help="Record a heartbeat")
    sp_hb.add_argument("project_id")
    sp_hb.add_argument("--pid", type=int, required=True)
    sp_hb.add_argument("--lease-token", required=True)
    sp_hb.set_defaults(func=_cmd_heartbeat)

    sp_kill = sub.add_parser("kill", help="Kill a specific T0 incarnation")
    sp_kill.add_argument("project_id")
    sp_kill.add_argument("--lease-token", required=True)
    sp_kill.add_argument("--sigkill", action="store_true", help="Use SIGKILL instead of SIGTERM")
    sp_kill.add_argument("--wait", type=float, default=None, help="Wait timeout seconds")
    sp_kill.set_defaults(func=_cmd_kill)

    sp_fr = sub.add_parser("force-release", help="Release a lease without process verification (operator escape)")
    sp_fr.add_argument("--lease-token", required=True)
    sp_fr.add_argument("--reason", required=True)
    sp_fr.set_defaults(func=_cmd_force_release)

    sp_list = sub.add_parser("list", help="List all running T0 leases")
    sp_list.add_argument("--json", action="store_true", help="Emit JSON")
    sp_list.set_defaults(func=_cmd_list)

    sp_reap = sub.add_parser("reap", help="Run reap loop once")
    sp_reap.set_defaults(func=_cmd_reap)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
