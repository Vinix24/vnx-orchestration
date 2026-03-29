#!/usr/bin/env python3
"""
CLI entry point for VNX Runtime Reconciler.

Detects expired leases, orphaned dispatch attempts, and unresolved dispatches
in the canonical runtime coordination database, then transitions them into
explicit recoverable states.

Usage:
    python scripts/runtime_reconciler_cli.py [OPTIONS]

Options:
    --state-dir PATH         Override state directory (default: .vnx-data/state/)
    --dry-run                Detect issues without modifying state
    --auto-recover-dispatches  Auto-recover timed_out/failed_delivery dispatches
    --no-auto-recover-leases   Don't auto-recover expired leases to idle
    --attempt-stale-seconds N  Orphaned attempt threshold (default: 300)
    --dispatch-stuck-seconds N Stuck dispatch threshold (default: 600)
    --max-attempts N           Max dispatch attempts before expiry (default: 3)
    --json                   Output result as JSON
    --verbose                Show detailed action log
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Add scripts/lib to path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_reconciler import ReconcilerConfig, RuntimeReconciler  # noqa: E402


def _resolve_state_dir(override: str | None) -> Path:
    if override:
        return Path(override)
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data) / "state"
    here = SCRIPT_DIR.parent
    return here / ".vnx-data" / "state"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VNX Runtime Reconciler — detect and recover stale runtime state"
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="Path to state directory containing runtime_coordination.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect issues without modifying state",
    )
    parser.add_argument(
        "--auto-recover-dispatches",
        action="store_true",
        help="Auto-recover timed_out/failed_delivery dispatches",
    )
    parser.add_argument(
        "--no-auto-recover-leases",
        action="store_true",
        help="Don't auto-recover expired leases to idle",
    )
    parser.add_argument(
        "--attempt-stale-seconds",
        type=int,
        default=300,
        help="Seconds before a pending/delivering attempt is orphaned (default: 300)",
    )
    parser.add_argument(
        "--dispatch-stuck-seconds",
        type=int,
        default=600,
        help="Seconds before a stuck dispatch times out (default: 600)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Max dispatch attempts before expiry (default: 3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output result as JSON",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed action log",
    )

    args = parser.parse_args()

    state_dir = _resolve_state_dir(args.state_dir)
    if not state_dir.exists():
        print(f"Error: state directory not found: {state_dir}", file=sys.stderr)
        return 1

    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        print("Run `python scripts/runtime_coordination_init.py` first.", file=sys.stderr)
        return 1

    config = ReconcilerConfig(
        auto_recover_expired_leases=not args.no_auto_recover_leases,
        auto_recover_dispatches=args.auto_recover_dispatches,
        attempt_stale_seconds=args.attempt_stale_seconds,
        dispatch_stuck_seconds=args.dispatch_stuck_seconds,
        max_dispatch_attempts=args.max_attempts,
    )

    reconciler = RuntimeReconciler(state_dir, config=config)
    result = reconciler.run(dry_run=args.dry_run)

    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.summary())

        if args.verbose and result.total_actions > 0:
            print("\nDetailed actions:")
            all_actions = (
                result.expired_leases
                + result.recovered_leases
                + result.timed_out_dispatches
                + result.recovered_dispatches
                + result.expired_dispatches
                + result.failed_attempts
            )
            for action in all_actions:
                print(f"  [{action.entity_type}] {action.entity_id}")
                print(f"    {action.from_state} -> {action.to_state} ({action.action})")
                print(f"    Reason: {action.reason}")

    return 0 if result.is_clean or result.total_actions > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
