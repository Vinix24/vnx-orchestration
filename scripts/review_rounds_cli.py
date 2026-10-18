#!/usr/bin/env python3
"""vnx review rounds — the round ledger of the PR review (R1).

``vnx review rounds rebuild --pr N`` rebuilds PR N's lines in the project's
round ledger (``state/review_gates/rounds.ndjson``) from its headless reports,
its review-gate receipts and its result records
(``scripts/lib/review_rounds_rebuild.py``). Run it for a PR reviewed before the
ledger existed (the merge door refuses its PASS until then) and after the gate
or the door reported the ledger unreadable for a PR. Idempotent: a second run
adds nothing.

Exit codes:
  0  rebuilt (the summary names what was added, kept and left out, and the B3 count)
  2  bad input, or the store could not be read or written

Usage:
  python3 scripts/review_rounds_cli.py rounds rebuild --pr 2080
  python3 scripts/review_rounds_cli.py rounds rebuild --pr 2080 --json
  python3 scripts/review_rounds_cli.py rounds rebuild --pr 2080 --data-dir /path/to/.vnx-data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXIT_OK = 0
EXIT_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx review", description="The round ledger of the PR review (R1)")
    sub = parser.add_subparsers(dest="noun", required=True)
    rounds = sub.add_parser("rounds", help="the round ledger: one line per review verdict, per seat, per head")
    verbs = rounds.add_subparsers(dest="verb", required=True)
    rebuild = verbs.add_parser("rebuild", help="rebuild a PR's ledger lines from its reports, receipts and results")
    rebuild.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    rebuild.add_argument("--data-dir", default=None, help="the project's .vnx-data (default: this project's)")
    rebuild.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _data_dir(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from vnx_paths import ensure_env

    return Path(ensure_env()["VNX_DATA_DIR"])


def _summary(result) -> List[str]:
    """The human summary of a ``review_rounds_rebuild.RebuildResult``."""
    from review_rounds_rebuild import QUARANTINE_SUFFIX

    lines = [
        f"round ledger {result.ledger}: PR #{result.pr_number}",
        f"  added {len(result.added)} line(s), kept {result.kept} already there",
    ]
    for line in result.added:
        lines.append(
            f"    + {line.recorded_at} {line.seat:<14} {line.reviewer:<14} {line.verdict.upper():<4} "
            f"blockers={line.blocking_count} head={line.head_sha[:12]}"
        )
    for item in result.skipped:
        lines.append(f"  left out {item.report}: {item.reason}")
    if result.dropped or result.quarantined:
        lines.append(
            f"  unreadable lines: {result.dropped} of this PR dropped, {result.quarantined} naming no PR "
            f"moved to {result.ledger.name}{QUARANTINE_SUFFIX}"
        )
    lines.append(f"  {result.cap.summary()}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    # After the sys.path bootstrap above: both live in scripts/lib.
    import review_rounds_rebuild
    from review_rounds import RoundLedgerError

    args = _build_parser().parse_args(argv)
    if args.pr <= 0:
        print("vnx review rounds rebuild: --pr must be a positive PR number", file=sys.stderr)
        return EXIT_ERROR
    try:
        result = review_rounds_rebuild.rebuild(args.pr, data_dir=_data_dir(args.data_dir))
    except (RoundLedgerError, OSError) as exc:
        print(f"vnx review rounds rebuild: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("\n".join(_summary(result)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
