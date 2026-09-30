#!/usr/bin/env python3
"""Janitor: drain decided dispatches from active/ to completed/ or dead_letter/.

The dispatcher moves a dispatch file from pending/ to active/ on successful
delivery, but nothing moves it out once a receipt is received.  Over time
active/ accumulates completed and orphaned directories that make it useless
as a "currently in-flight" worklist.

A dispatch is only finished with an outcome (operator decision 29-09-2026,
fabric-state-herstel D4b2): accept or reject. Without one a T0 investigates;
nothing is closed silently. The rule is ``open_outcomes.active_destination``,
the same one the ``active_dispatch`` open points are read with.

Rules
-----
* a T0 recorded a decision about the dispatch (t0_decision_log.jsonl,
  read by open_outcomes.read_outcome_decisions): accept → completed/,
  reject → dead_letter/. The decision wins over the receipts, and a T0
  reject is the only way into dead_letter/.
* dispatch's outcome is accept (success)                              → move to completed/
* anything else stays in active/ as an open point in t0_index.json
  open_outcomes until a T0 decides:
  - the outcome is reject (failure) or investigate (missing verification,
    an open blocker, a status literal nobody knows)
  - a receipt but no outcome of its own (only evidence, superseded by a
    child, its lines attributed to another dispatch or dropped as an
    unlinked gate run)
  - no receipt AND older than --older-than-hours (default 1), or no timestamp
* dispatch has no receipt AND is newer than the threshold             → leave alone (still running)

Failed dispatches must NEVER be drained as completed: the dispatch's outcome
(``receipt_outcome``) is consulted, so that a failed, timed-out or
contract-invalid dispatch stays open instead of masquerading as successful
work. A failure that a later successful retry replaced does not.

Exit codes
----------
0  all OK (or dry-run summary printed with nothing remaining)
1  one or more moves failed
2  bad arguments / IO error on startup

Usage
-----
    python3 scripts/check_active_drain.py [--dry-run] [--data-dir PATH] [--older-than-hours N]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from open_outcomes import (  # noqa: E402
    DECISION_LOG_NAME,
    NO_RECEIPT_THRESHOLD_HOURS,
    DispatchEntry,
    active_destination,
    iter_active_dispatches,
    read_outcome_decisions,
    receipt_presence,
    receipt_status_index,
    scoped_processed,
)
from project_root import resolve_data_dir  # noqa: E402
from receipt_outcome import summarize as summarize_outcomes  # noqa: E402


def _data_dir(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    return resolve_data_dir(__file__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class DrainResult(NamedTuple):
    dispatch_id: str
    action: str          # "completed" | "dead_letter" | "skipped" | "error"
    reason: str
    dry_run: bool


# ---------------------------------------------------------------------------
# Receipt index
# ---------------------------------------------------------------------------

# What a dispatch's processed receipts add up to is receipt_outcome's call
# (fabric-state-herstel D3/D4a); whether it has a receipt at all is
# open_outcomes.receipt_presence. The janitor and dispatch_cleanup read the
# same two, so there is no third reading of "this dispatch is done".


def build_receipt_index(receipts_dir: Path) -> frozenset[str]:
    """Return the set of dispatch_ids with an outcome of their own in
    receipts/processed/ (the keys of build_receipt_status_index); a receipt on
    disk without an outcome is build_receipt_presence's.

    Kept for backwards compatibility — callers that need success/failure
    discrimination should use build_receipt_status_index instead.
    """
    return frozenset(build_receipt_status_index(receipts_dir).keys())


def build_receipt_status_index(receipts_dir: Path) -> dict[str, str]:
    """Map dispatch_id → \"success\" | \"failure\" | \"investigate\", one per dispatch.

    ``receipts_dir`` is ``<data dir>/receipts``. A dispatch without an outcome
    of its own (only bookkeeping, noise, evidence, another project's lines, or
    superseded by a child) has no entry.
    """
    receipts, project_id = scoped_processed(receipts_dir)
    return receipt_status_index(summarize_outcomes(receipts, project_id=project_id))


def build_receipt_presence(receipts_dir: Path) -> frozenset[str]:
    """The dispatch ids of this project with a processed receipt on disk,
    outcome or not (open_outcomes.receipt_presence)."""
    receipts, project_id = scoped_processed(receipts_dir)
    return receipt_presence(receipts, project_id,
                            summarize_outcomes(receipts, project_id=project_id))


# ---------------------------------------------------------------------------
# Core drain logic
# ---------------------------------------------------------------------------

def _destination(
    entry: DispatchEntry,
    receipt_status: str | None,
    now: datetime,
    older_than_seconds: float,
    has_receipt: bool = False,
    decision: str | None = None,
) -> tuple[str, str]:
    """Where a dispatch goes and why: ``completed``, ``dead_letter`` or
    ``skipped`` (it stays in active/); open_outcomes.active_destination."""
    age = (now - entry.timestamp).total_seconds() if entry.timestamp is not None else None
    destination, reason, _ = active_destination(
        receipt_status=receipt_status, has_receipt=has_receipt, decision=decision,
        age_seconds=age, threshold_seconds=older_than_seconds)
    return destination, reason


def drain_one(
    entry: DispatchEntry,
    receipt_index: "frozenset[str] | dict[str, str]",
    dispatches_dir: Path,
    now: datetime,
    older_than_seconds: float,
    dry_run: bool,
    receipt_present: "frozenset[str]" = frozenset(),
    decisions: "Mapping[str, dict] | None" = None,
) -> DrainResult:
    """``receipt_present`` (build_receipt_presence) names the dispatches with a
    receipt on disk; one of them without an index entry is left in active/.
    ``decisions`` (read_outcome_decisions) maps a dispatch id to the T0's last
    recorded decision, which moves the dispatch whatever its receipts say."""
    # Accept either the legacy frozenset (success-implied) or the new
    # status-aware dict to keep external callers working.
    if isinstance(receipt_index, dict):
        receipt_status = receipt_index.get(entry.dispatch_id)
    elif entry.dispatch_id in receipt_index:
        receipt_status = "success"
    else:
        receipt_status = None

    decision = ((decisions or {}).get(entry.dispatch_id) or {}).get("decision")
    dest_bucket, reason = _destination(entry, receipt_status, now, older_than_seconds,
                                       has_receipt=entry.dispatch_id in receipt_present,
                                       decision=decision)
    if dest_bucket == "skipped":
        return DrainResult(
            dispatch_id=entry.dispatch_id,
            action="skipped",
            reason=reason,
            dry_run=dry_run,
        )

    if dry_run:
        return DrainResult(
            dispatch_id=entry.dispatch_id,
            action=dest_bucket,
            reason=f"[dry-run] would move: {reason}",
            dry_run=True,
        )

    dest_dir = dispatches_dir / dest_bucket
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / entry.directory.name
    if dest.exists():
        return DrainResult(
            dispatch_id=entry.dispatch_id,
            action=dest_bucket,
            reason=f"[skip] destination already exists: {dest.name}",
            dry_run=False,
        )
    try:
        shutil.move(str(entry.directory), str(dest))
    except (OSError, shutil.Error) as exc:
        return DrainResult(
            dispatch_id=entry.dispatch_id,
            action="error",
            reason=f"move failed: {exc}",
            dry_run=dry_run,
        )

    return DrainResult(
        dispatch_id=entry.dispatch_id,
        action=dest_bucket,
        reason=reason,
        dry_run=False,
    )


def drain_active(
    data_dir: Path,
    older_than_hours: float = NO_RECEIPT_THRESHOLD_HOURS,
    dry_run: bool = False,
) -> list[DrainResult]:
    """Main entry point: drain active/ dispatches. Returns list of DrainResult."""
    dispatches_dir = data_dir / "dispatches"
    receipts_dir = data_dir / "receipts"

    receipts, project_id = scoped_processed(receipts_dir)
    summary = summarize_outcomes(receipts, project_id=project_id)
    receipt_index = receipt_status_index(summary)
    receipt_present = receipt_presence(receipts, project_id, summary)
    # the sentinel project matches no decision: without a project id nothing is decided
    decisions = read_outcome_decisions(data_dir / "state" / DECISION_LOG_NAME, project_id)
    now = datetime.now(tz=timezone.utc)
    older_than_seconds = older_than_hours * 3600.0

    results: list[DrainResult] = []
    for entry in iter_active_dispatches(dispatches_dir):
        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=dispatches_dir,
            now=now,
            older_than_seconds=older_than_seconds,
            dry_run=dry_run,
            receipt_present=receipt_present,
            decisions=decisions,
        )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _non_negative_hours(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}")
    if v < 0:
        raise argparse.ArgumentTypeError(f"--older-than-hours must be >= 0, got {v}")
    return v


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Drain decided dispatches from active/ to completed/ or dead_letter/.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report what would be moved without moving.")
    p.add_argument("--data-dir", metavar="PATH", help="Override VNX data dir (default: auto-resolved .vnx-data).")
    p.add_argument("--older-than-hours", type=_non_negative_hours, default=NO_RECEIPT_THRESHOLD_HOURS,
                   metavar="N",
                   help="Open-point threshold: a dispatch without a receipt older than N hours is an "
                        "open point for a T0, never dead-lettered (default: 1.0). Must be >= 0.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        data_dir = _data_dir(args.data_dir)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results = drain_active(
        data_dir=data_dir,
        older_than_hours=args.older_than_hours,
        dry_run=args.dry_run,
    )

    if not results:
        print("active/ is empty — nothing to drain.")
        return 0

    errors = 0
    for r in results:
        tag = "[DRY-RUN] " if r.dry_run else ""
        print(f"{tag}{r.action.upper():12s} {r.dispatch_id}  ({r.reason})")
        if r.action == "error":
            errors += 1

    counts = {a: sum(1 for r in results if r.action == a) for a in ("completed", "dead_letter", "skipped", "error")}
    print(
        f"\nSummary: completed={counts['completed']} dead_letter={counts['dead_letter']} "
        f"skipped={counts['skipped']} errors={counts['error']}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
