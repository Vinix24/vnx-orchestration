#!/usr/bin/env python3
"""Receipt-driven reconciliation of dispatcher's active/ directory.

Replaces the mtime-only "stuck file" heuristic in dispatcher_minimal.sh.
That heuristic moved any *.md older than 60 minutes from active/ to
completed/, which silently misclassified legitimate long-running tasks
(file mtime is set at delivery time and never refreshed) as completed and
hid live work from T0 state.

Reconciliation rules (fabric-state-herstel D4b2)
------------------------------------------------
A dispatch is only finished with an outcome. The rule is
``open_outcomes.active_destination``, the same one ``check_active_drain.py``
drains directories with; the receipts are read the same way
(``open_outcomes.dispatch_receipts``: ``receipt_outcome`` plus
``receipt_presence``), scoped to this store's project (ADR-007).

- T0 decision accept, or receipt outcome accept          → move to completed/
- T0 decision reject                                     → move to dead_letter/
- No receipt + age >= stale_hours                        → orphan (open point, file stays)
- Failure/investigate outcome, or a receipt without an
  outcome of its own                                     → open (open point, file stays)
- Otherwise                                              → skipped (file stays)
- A .md without a [[TARGET:...]] marker (a README)       → ignored (no dispatch, file stays)

A receipt of any kind used to promote the file, so a failure-only dispatch
went to completed/. The store is ``receipts_processed_dir.parent.parent``:
its ``state/t0_decision_log.jsonl`` carries the T0 decisions.

CLI
---
    python3 active_dispatch_janitor.py \
        --active-dir <dir> --completed-dir <dir> \
        --receipts-processed-dir <dir> [--stale-hours N] [--json]

Exit codes
----------
0  reconciliation finished (possibly with orphans)
1  one or more move operations failed
2  bad CLI args
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from open_outcomes import (  # noqa: E402
    DECISION_LOG_NAME,
    OUTCOME_NO_RECEIPT,
    active_destination,
    dispatch_receipts,
    markdown_dispatch,
    read_outcome_decisions,
    scoped_processed,
)


@dataclass(frozen=True)
class ReconcileResult:
    dispatch_id: str
    action: str   # "completed" | "dead_letter" | "orphan" | "open" | "skipped" | "ignored" | "error"
    reason: str


def build_receipt_index(receipts_processed_dir: Path) -> frozenset[str]:
    """The dispatch_ids of this store's project with a receipt in
    receipts/processed/*.json, outcome or not (open_outcomes.receipt_presence):
    bookkeeping, test noise and another project's lines are no receipt."""
    receipts, project_id = scoped_processed(Path(receipts_processed_dir).parent)
    return dispatch_receipts(receipts, project_id)[1]


def _move(path: Path, dest_dir: Path, did: str, action: str, reason: str) -> ReconcileResult:
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
    except (OSError, shutil.Error) as exc:
        return ReconcileResult(did, "error", f"move failed: {exc}")
    return ReconcileResult(did, action, reason)


def reconcile_active(
    active_dir: Path,
    completed_dir: Path,
    receipts_processed_dir: Path,
    stale_hours: float = 24.0,
    now_ts: Optional[float] = None,
    dead_letter_dir: Optional[Path] = None,
) -> list[ReconcileResult]:
    """Reconcile active/*.md files against the outcome of their receipts.

    Never moves a file out of active/ on age, and never on a receipt alone:
    only an accept promotes to completed/, only a T0 reject goes to
    ``dead_letter_dir`` (default: next to ``completed_dir``). Everything else
    stays in active/ as an open point; orphans (no receipt, age >=
    stale_hours) are reported as ``orphan`` so callers can log them.
    """
    if not active_dir.is_dir():
        return []
    receipts_dir = Path(receipts_processed_dir).parent
    receipts, project_id = scoped_processed(receipts_dir)
    status_index, presence = dispatch_receipts(receipts, project_id)
    decisions = read_outcome_decisions(receipts_dir.parent / "state" / DECISION_LOG_NAME, project_id)
    dead_letter = dead_letter_dir if dead_letter_dir is not None else completed_dir.parent / "dead_letter"
    now = time.time() if now_ts is None else now_ts
    # stale_hours <= 0 never reports an orphan (the CLI's historical meaning)
    stale_seconds = stale_hours * 3600.0 if stale_hours > 0 else float("inf")
    results: list[ReconcileResult] = []
    for path in sorted(active_dir.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        # the drain's reading of a dispatch file (open_outcomes.markdown_dispatch)
        entry, not_a_dispatch = markdown_dispatch(path)
        if entry is None:
            results.append(ReconcileResult(path.name, "ignored", not_a_dispatch))
            continue
        did = entry.dispatch_id
        age = now - entry.timestamp.timestamp() if entry.timestamp is not None else None
        destination, reason, open_as = active_destination(
            receipt_status=status_index.get(did), has_receipt=did in presence,
            decision=(decisions.get(did) or {}).get("decision"),
            age_seconds=age, threshold_seconds=stale_seconds)
        if destination == "completed":
            results.append(_move(path, completed_dir, did, "completed", reason))
        elif destination == "dead_letter":
            results.append(_move(path, dead_letter, did, "dead_letter", reason))
        elif open_as == OUTCOME_NO_RECEIPT:
            results.append(ReconcileResult(did, "orphan", reason))
        elif open_as is not None:
            results.append(ReconcileResult(did, "open", reason))
        else:
            results.append(ReconcileResult(did, "skipped", reason))
    return results


def _format_human(results: Iterable[ReconcileResult]) -> str:
    return "\n".join(f"{r.action.upper():10s} {r.dispatch_id}  ({r.reason})" for r in results)


def _format_json(results: Iterable[ReconcileResult]) -> str:
    return json.dumps(
        [{"dispatch_id": r.dispatch_id, "action": r.action, "reason": r.reason} for r in results],
        separators=(",", ":"),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--active-dir", required=True)
    parser.add_argument("--completed-dir", required=True)
    parser.add_argument("--receipts-processed-dir", required=True)
    parser.add_argument("--dead-letter-dir", default=None,
                        help="Destination of a T0 reject (default: dead_letter/ next to --completed-dir).")
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=24.0,
        help="Age (hours) above which a receiptless dispatch is reported as orphan (default: 24).",
    )
    parser.add_argument("--json", action="store_true", help="Emit results as JSON to stdout.")
    args = parser.parse_args(argv)

    results = reconcile_active(
        Path(args.active_dir),
        Path(args.completed_dir),
        Path(args.receipts_processed_dir),
        stale_hours=args.stale_hours,
        dead_letter_dir=Path(args.dead_letter_dir) if args.dead_letter_dir else None,
    )
    if args.json:
        print(_format_json(results))
    else:
        if results:
            print(_format_human(results))
    return 1 if any(r.action == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
