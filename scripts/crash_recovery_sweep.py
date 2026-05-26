#!/usr/bin/env python3
"""CLI: flood-safe crash-recovery sweep for orphaned active dispatches.

OPT-IN. This is **not** wired into the dispatch hot path. Run it manually after
a terminal/orchestrator crash, or from a deliberately enabled supervisor tick,
to recover ``.vnx-data/dispatches/active/`` entries whose orchestrator process
(``terminal_leases.worker_pid``, PR #636) is no longer alive.

Flood-safety guarantees (see scripts/lib/crash_recovery_sweep.py for detail):
  * capped at --max-orphans recoveries per run (default 10),
  * idempotent (recovered orphans leave active/ and the receipt writer dedups),
  * --dry-run mutates nothing.

Examples:
    # See what would be recovered, write nothing:
    python3 scripts/crash_recovery_sweep.py --dry-run --json

    # Recover up to 10 dead-PID orphans (default cap):
    python3 scripts/crash_recovery_sweep.py

    # Recover at most 3 this run:
    python3 scripts/crash_recovery_sweep.py --max-orphans 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from crash_recovery_sweep import DEFAULT_MAX_ORPHANS, sweep  # noqa: E402


def _default_data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return _HERE.parent / ".vnx-data"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crash_recovery_sweep",
        description=(
            "Recover orphaned active dispatches left by a dead orchestrator "
            "(opt-in, capped, idempotent, dry-run aware)."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to .vnx-data (default: $VNX_DATA_DIR or repo .vnx-data).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Path to runtime state dir (default: <data-dir>/state).",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
        help="Project id for the lease PID lookup (default: $VNX_PROJECT_ID or vnx-dev).",
    )
    parser.add_argument(
        "--max-orphans",
        type=int,
        default=DEFAULT_MAX_ORPHANS,
        help=f"Max orphans to recover per run (flood cap, default {DEFAULT_MAX_ORPHANS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify orphans and report; write nothing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the SweepResult as JSON on stdout.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable INFO logging to stderr.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if args.max_orphans < 1:
        parser.error("--max-orphans must be >= 1")

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else _default_data_dir()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else None

    result = sweep(
        data_dir,
        state_dir=state_dir,
        project_id=args.project_id,
        max_orphans=args.max_orphans,
        dry_run=args.dry_run,
    )

    if args.json:
        json.dump(result.to_dict(), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
    else:
        verb = "would recover" if args.dry_run else "recovered"
        print(
            f"crash_recovery_sweep: scanned {result.scanned}, {verb} "
            f"{len(result.recovered)}, skipped {len(result.skipped_alive)} alive"
            + (f", CAPPED at {args.max_orphans}" if result.capped else "")
            + (f", {len(result.errors)} error(s)" if result.errors else "")
        )

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
