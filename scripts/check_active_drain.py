#!/usr/bin/env python3
"""Janitor: drain stale dispatches from active/ to completed/ or dead_letter/.

The dispatcher moves a dispatch file from pending/ to active/ on successful
delivery, but nothing moves it out once a receipt is received.  Over time
active/ accumulates completed and orphaned directories that make it useless
as a "currently in-flight" worklist.

Rules
-----
* dispatch has a matching receipt in receipts/processed/  → move to completed/
* dispatch has no receipt AND is older than --older-than-hours (default 1)   → move to dead_letter/
* dispatch has no receipt AND is newer than the threshold                     → leave alone

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

from project_root import resolve_data_dir  # noqa: E402


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

def build_receipt_index(receipts_dir: Path) -> frozenset[str]:
    """Return the set of dispatch_ids present in receipts/processed/."""
    processed = receipts_dir / "processed"
    if not processed.is_dir():
        return frozenset()

    ids: set[str] = set()
    for path in processed.iterdir():
        if not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            did = data.get("dispatch_id", "")
            if did and did != "unknown":
                ids.add(did)
        except (json.JSONDecodeError, OSError):
            continue
    return frozenset(ids)


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

def drain_one(
    entry: DispatchEntry,
    receipt_index: frozenset[str],
    dispatches_dir: Path,
    now: datetime,
    older_than_seconds: float,
    dry_run: bool,
) -> DrainResult:
    has_receipt = entry.dispatch_id in receipt_index

    if has_receipt:
        dest_bucket = "completed"
        reason = "receipt found"
    else:
        if entry.timestamp is None:
            # No timestamp → treat as orphaned regardless of age
            dest_bucket = "dead_letter"
            reason = "no receipt, no timestamp (orphaned)"
        else:
            age = (now - entry.timestamp).total_seconds()
            if age >= older_than_seconds:
                dest_bucket = "dead_letter"
                reason = f"no receipt, age {age / 3600:.1f}h > threshold"
            else:
                return DrainResult(
                    dispatch_id=entry.dispatch_id,
                    action="skipped",
                    reason=f"no receipt yet, age {age / 3600:.2f}h < threshold",
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

    receipt_index = build_receipt_index(receipts_dir)
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

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Drain stale dispatches from active/ to completed/ or dead_letter/.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report what would be moved without moving.")
    p.add_argument("--data-dir", metavar="PATH", help="Override VNX data dir (default: auto-resolved .vnx-data).")
    p.add_argument("--older-than-hours", type=float, default=1.0, metavar="N",
                   help="Dead-letter threshold: orphan dispatches older than N hours (default: 1.0).")
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
