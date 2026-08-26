#!/usr/bin/env python3
"""CLI: idempotent orphan sweep for teardown leftovers (OI-1192).

OPT-IN. Not wired into the dispatch hot path. Run it manually after a wrapper
crash, or from a deliberately enabled supervisor tick, to clean/mark the three
kinds of objects in-process teardown would have removed but a killed wrapper
left behind:

  * tmux sessions ``vnx-<id>`` whose worker pane is provably dead,
  * worktrees ``<repo>/.vnx-data/worktrees/dispatch-<id>`` whose tmux session is
    gone (dirty worktrees are LOCKED/marked, never deleted),
  * ``dispatches/active/<id>/manifest.json`` entries whose orchestrator is gone
    (delegated to crash_recovery_sweep).

Safety (see scripts/lib/orphan_sweep.py for the full contract):
  * fail-open liveness — "cannot measure" is never read as "dead",
  * idempotent — every action is a no-op on a second run,
  * never touches the invoking dispatch (``$VNX_CURRENT_DISPATCH_ID``).

Examples:
    # See what would be cleaned/marked, write nothing:
    python3 scripts/orphan_sweep.py --dry-run --json

    # Clean/mark everything that is provably orphaned:
    python3 scripts/orphan_sweep.py

    # Cap the active-manifest recovery at 3 this run:
    python3 scripts/orphan_sweep.py --max-orphans 3
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

from orphan_sweep import DEFAULT_MAX_ORPHANS, sweep  # noqa: E402


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orphan_sweep",
        description=(
            "Idempotently clean/mark orphaned teardown leftovers "
            "(tmux sessions, worktrees, active manifests)."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Main repo root (worktrees live under <root>/.vnx-data/worktrees; "
             "default: resolved project root).",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to .vnx-data for register/receipts/active-manifest reads "
             "(default: resolved via the fabric's canonical central-store "
             "resolver, typically ~/.vnx-data/<project_id>).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Path to runtime state dir (default: <data-dir>/state when "
             "--data-dir is given; otherwise the fabric's canonical "
             "central-store resolver, same as --data-dir).",
    )
    parser.add_argument(
        "--account-data-root",
        default=None,
        help="Account-wide root under which every project's central store "
             "lives, for the kind-1b OI-1424 cross-project owner lookup "
             "(<root>/<project_id>/state/...). Default: $VNX_DATA_HOME or "
             "~/.vnx-data.",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
        help="Project id for the lease PID lookup (default: $VNX_PROJECT_ID or vnx-dev).",
    )
    parser.add_argument(
        "--current-dispatch",
        default=os.environ.get("VNX_CURRENT_DISPATCH_ID", "").strip() or None,
        help="Dispatch id to fence off (default: $VNX_CURRENT_DISPATCH_ID).",
    )
    parser.add_argument(
        "--max-orphans",
        type=int,
        default=DEFAULT_MAX_ORPHANS,
        help=f"Max active manifests to recover per run (flood cap, default {DEFAULT_MAX_ORPHANS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify orphans and report; write nothing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the OrphanSweepResult as JSON on stdout.",
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

    repo_root = Path(args.repo_root).expanduser() if args.repo_root else None
    # Neither flag defaults eagerly here: an explicit --data-dir/--state-dir
    # always wins, but omitting BOTH must reach sweep() as None so it resolves
    # the register/receipts/active-manifest store via the fabric's canonical
    # central-store resolver (scripts/lib/orphan_sweep.py::_resolve_central_paths)
    # instead of this CLI's own (formerly repo-relative) guess.
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else None
    account_data_root = (
        Path(args.account_data_root).expanduser() if args.account_data_root else None
    )

    result = sweep(
        repo_root=repo_root,
        data_dir=data_dir,
        state_dir=state_dir,
        project_id=args.project_id,
        current_dispatch_id=args.current_dispatch,
        max_orphans=args.max_orphans,
        dry_run=args.dry_run,
        account_data_root=account_data_root,
    )

    if args.json:
        json.dump(result.to_dict(), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
    else:
        verb = "would" if args.dry_run else "did"
        print(
            f"orphan_sweep: {verb} kill {len(result.tmux_killed)} tmux session(s), "
            f"reap {len(result.worktrees_removed)} worktree(s), "
            f"mark {len(result.worktrees_preserved)} dirty worktree(s), "
            f"recover {len(result.active_recovered)} active manifest(s)"
            + (f" (CAPPED at {args.max_orphans})" if result.active_capped else "")
            + (f"; {len(result.errors)} error(s)" if result.errors else "")
        )

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
