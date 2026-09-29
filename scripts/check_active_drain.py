#!/usr/bin/env python3
"""Janitor: drain stale dispatches from active/ to completed/ or dead_letter/.

The dispatcher moves a dispatch file from pending/ to active/ on successful
delivery, but nothing moves it out once a receipt is received.  Over time
active/ accumulates completed and orphaned directories that make it useless
as a "currently in-flight" worklist.

Rules
-----
* dispatch's outcome is accept (success)                              → move to completed/
* dispatch's outcome is reject (failure)                              → move to dead_letter/
* dispatch's outcome is investigate (missing verification, an open
  blocker, a status literal nobody knows)                             → leave alone
* dispatch has no receipt AND is older than --older-than-hours (default 1)   → move to dead_letter/
* dispatch has no receipt AND is newer than the threshold                     → leave alone

Failed dispatches must NEVER be drained as completed: the dispatch's outcome
(``receipt_outcome``) is consulted, so that a failed, timed-out or
contract-invalid dispatch routes to dead_letter/ instead of masquerading as
successful work. A failure that a later successful retry replaced does not.

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
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from project_root import resolve_data_dir, resolve_project_id  # noqa: E402
from receipt_outcome import summarize as summarize_outcomes  # noqa: E402
from vnx_paths import project_id_from_state_dir  # noqa: E402


def _data_dir(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    return resolve_data_dir(__file__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class DispatchEntry(NamedTuple):
    dispatch_id: str
    directory: Path
    timestamp: datetime | None


class DrainResult(NamedTuple):
    dispatch_id: str
    action: str          # "completed" | "dead_letter" | "skipped" | "error"
    reason: str
    dry_run: bool


# ---------------------------------------------------------------------------
# Receipt index
# ---------------------------------------------------------------------------

# What a dispatch's processed receipts add up to is receipt_outcome's call
# (fabric-state-herstel D3/D4a): the last lane status in file order, a later
# contract_invalid as a failure, noise, bookkeeping and other projects ignored.
# The drain used to let any failure win, so an old failure outlived a
# successful retry.
_UNSCOPED_PROJECT = "\x00unscoped"

# The index holds only dispatches with an outcome of their own. ``unknown``
# (only evidence, no lane line) and ``superseded`` (a child took the work over)
# are no outcome: such a dispatch has no entry and falls through to the age
# rule for dispatches without a receipt, as before D4a. A receipt whose status
# literal nobody knows reads as ``investigate`` (receipt_verdict), so it stays
# in active/ for a human rather than going to completed/ or dead_letter/.
_INDEX_STATUS = {"accept": "success", "reject": "failure", "investigate": "investigate"}


def _project_id(data_dir: Path) -> str:
    """The project whose store this is: derived from the data dir, else ambient."""
    derived = project_id_from_state_dir(data_dir / "state")
    if derived:
        return derived
    try:
        return resolve_project_id()
    except RuntimeError:
        return ""


def build_receipt_index(receipts_dir: Path) -> frozenset[str]:
    """Return the set of dispatch_ids present in receipts/processed/.

    Kept for backwards compatibility — callers that need success/failure
    discrimination should use build_receipt_status_index instead.
    """
    return frozenset(build_receipt_status_index(receipts_dir).keys())


def _read_processed(processed: Path) -> list[dict]:
    """Processed receipts in file-name order: the names start with the write
    time in epoch seconds, so this is the order they arrived in."""
    receipts: list[dict] = []
    for path in sorted(processed.iterdir(), key=lambda p: p.name):
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            receipts.append(data)
    return receipts


def build_receipt_status_index(receipts_dir: Path) -> dict[str, str]:
    """Map dispatch_id → \"success\" | \"failure\" | \"investigate\", one per dispatch.

    ``investigate`` (missing verification, an open blocker, an unknown status
    literal) is not a result: the dispatch stays in active/ until a human has
    looked.

    ``receipts_dir`` is ``<data dir>/receipts``. A dispatch without an outcome
    of its own (only bookkeeping, noise, evidence, another project's lines, or
    superseded by a child) has no entry.
    """
    processed = receipts_dir / "processed"
    if not processed.is_dir():
        return {}

    receipts = _read_processed(processed)
    project_id = _project_id(receipts_dir.parent)
    if not project_id:
        receipts = [{k: v for k, v in r.items() if k != "project_id"} for r in receipts]
    summary = summarize_outcomes(receipts, project_id=project_id or _UNSCOPED_PROJECT)
    return {o["dispatch_id"]: _INDEX_STATUS[o["decision"]]
            for o in summary["outcomes"] if o["decision"] in _INDEX_STATUS}


# ---------------------------------------------------------------------------
# Active dispatch enumeration
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: str) -> datetime | None:
    """Parse ISO-8601 timestamp. Always returns timezone-aware UTC datetime."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        # Normalize: ensure tzinfo is set. Z-suffixed formats parse as naive on
        # some platforms, so force UTC. Already-aware datetimes pass through.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def iter_active_dispatches(dispatches_dir: Path) -> Iterator[DispatchEntry]:
    """Yield DispatchEntry for each directory under dispatches/active/."""
    active = dispatches_dir / "active"
    if not active.is_dir():
        return

    for entry_dir in sorted(active.iterdir()):
        if not entry_dir.is_dir():
            continue
        manifest = entry_dir / "manifest.json"
        dispatch_id = entry_dir.name
        timestamp: datetime | None = None
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                dispatch_id = data.get("dispatch_id", dispatch_id)
                raw_ts = data.get("timestamp", "")
                if raw_ts:
                    timestamp = _parse_timestamp(raw_ts)
            except (json.JSONDecodeError, OSError):
                pass
        yield DispatchEntry(dispatch_id=dispatch_id, directory=entry_dir, timestamp=timestamp)


# ---------------------------------------------------------------------------
# Core drain logic
# ---------------------------------------------------------------------------

def _destination(
    entry: DispatchEntry,
    receipt_status: str | None,
    now: datetime,
    older_than_seconds: float,
) -> tuple[str, str]:
    """Where a dispatch goes and why: ``completed``, ``dead_letter`` or
    ``skipped`` (it stays in active/)."""
    if receipt_status == "success":
        return "completed", "receipt found with success status"
    if receipt_status == "failure":
        return "dead_letter", "receipt found with failure status"
    if receipt_status is not None:
        # investigate, or a status a caller's own index carries that is not a
        # result: never completed work, never a failure either; a human looks.
        return "skipped", (f"outcome {receipt_status!r} needs a human look "
                           "(missing verification, open blocker or unknown status)")
    if entry.timestamp is None:
        # No timestamp → treat as orphaned regardless of age
        return "dead_letter", "no receipt, no timestamp (orphaned)"
    age = (now - entry.timestamp).total_seconds()
    if age >= older_than_seconds:
        return "dead_letter", f"no receipt, age {age / 3600:.1f}h > threshold"
    return "skipped", f"no receipt yet, age {age / 3600:.2f}h < threshold"


def drain_one(
    entry: DispatchEntry,
    receipt_index: "frozenset[str] | dict[str, str]",
    dispatches_dir: Path,
    now: datetime,
    older_than_seconds: float,
    dry_run: bool,
) -> DrainResult:
    # Accept either the legacy frozenset (success-implied) or the new
    # status-aware dict to keep external callers working.
    if isinstance(receipt_index, dict):
        receipt_status = receipt_index.get(entry.dispatch_id)
    elif entry.dispatch_id in receipt_index:
        receipt_status = "success"
    else:
        receipt_status = None

    dest_bucket, reason = _destination(entry, receipt_status, now, older_than_seconds)
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
    older_than_hours: float = 1.0,
    dry_run: bool = False,
) -> list[DrainResult]:
    """Main entry point: drain active/ dispatches. Returns list of DrainResult."""
    dispatches_dir = data_dir / "dispatches"
    receipts_dir = data_dir / "receipts"

    receipt_index = build_receipt_status_index(receipts_dir)
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
        description="Drain stale dispatches from active/ to completed/ or dead_letter/.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report what would be moved without moving.")
    p.add_argument("--data-dir", metavar="PATH", help="Override VNX data dir (default: auto-resolved .vnx-data).")
    p.add_argument("--older-than-hours", type=_non_negative_hours, default=1.0, metavar="N",
                   help="Dead-letter threshold: orphan dispatches older than N hours (default: 1.0). Must be >= 0.")
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
