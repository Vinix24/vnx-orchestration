#!/usr/bin/env python3
"""compact_state.py — VNX state file rotation and compaction.

Modes:
  intelligence_archive  Rotate t0_intelligence_archive.ndjson (skip <50MB, keep 7d)
  receipts              Cap t0_receipts.ndjson at 10000 records, archive overflow
  open_items_digest     Evict open_items_digest.json entries with last_updated >30d
  all                   Run all three modes
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from project_root import resolve_data_dir

INTELLIGENCE_ARCHIVE_MIN_MB = 50
INTELLIGENCE_ARCHIVE_KEEP_DAYS = 7
RECEIPTS_MAX_RECORDS = 10_000
OPEN_ITEMS_STALE_DAYS = 30


def _emit(level: str, code: str, **fields: object) -> None:
    payload: dict = {
        "level": level,
        "code": code,
        "timestamp": int(time.time()),
    }
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _archive_path(state_dir: Path, stem: str) -> Path:
    date_str = datetime.date.today().isoformat()
    return state_dir / "archive" / f"{stem}_{date_str}.ndjson.gz"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compact_intelligence_archive(state_dir: Path, *, dry_run: bool = False) -> int:
    """Rotate t0_intelligence_archive.ndjson. Returns 0 on success, non-zero on error."""
    live_file = state_dir / "t0_intelligence_archive.ndjson"

    if not live_file.exists():
        _emit("INFO", "intelligence_archive_skip", reason="file_not_found", path=str(live_file))
        return 0

    size_bytes = live_file.stat().st_size
    min_bytes = INTELLIGENCE_ARCHIVE_MIN_MB * 1024 * 1024

    if size_bytes < min_bytes:
        _emit(
            "INFO",
            "intelligence_archive_skip",
            reason="below_threshold_mb",
            size_mb=round(size_bytes / 1024 / 1024, 2),
            threshold_mb=INTELLIGENCE_ARCHIVE_MIN_MB,
        )
        return 0

    archive_file = _archive_path(state_dir, "t0_intelligence_archive")
    if archive_file.exists():
        _emit(
            "INFO",
            "intelligence_archive_skip",
            reason="archive_already_exists_today",
            archive=str(archive_file),
        )
        return 0

    cutoff_ts = time.time() - INTELLIGENCE_ARCHIVE_KEEP_DAYS * 86400
    raw = live_file.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines(keepends=True)

    keep: list[str] = []
    archive: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            keep.append(line)
            continue
        try:
            record = json.loads(stripped)
            ts = record.get("timestamp")
            if ts is not None and float(ts) < cutoff_ts:
                archive.append(line)
            else:
                keep.append(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            keep.append(line)

    if not archive:
        _emit("INFO", "intelligence_archive_skip", reason="all_records_within_7d", total_lines=len(lines))
        return 0

    _emit(
        "INFO",
        "intelligence_archive_rotating",
        live_path=str(live_file),
        archive_path=str(archive_file),
        archive_lines=len(archive),
        keep_lines=len(keep),
        dry_run=dry_run,
    )

    if dry_run:
        return 0

    try:
        _atomic_write_bytes(archive_file, gzip.compress("".join(archive).encode("utf-8")))
        _atomic_write_text(live_file, "".join(keep))
    except OSError as exc:
        _emit("ERROR", "intelligence_archive_io_error", error=str(exc))
        return 1

    _emit(
        "INFO",
        "intelligence_archive_done",
        archive=str(archive_file),
        archive_lines=len(archive),
        live_lines=len(keep),
    )
    return 0


def compact_receipts(state_dir: Path, *, dry_run: bool = False) -> int:
    """Cap t0_receipts.ndjson at 10000 lines, archive overflow. Returns 0 on success."""
    live_file = state_dir / "t0_receipts.ndjson"

    if not live_file.exists():
        _emit("INFO", "receipts_skip", reason="file_not_found", path=str(live_file))
        return 0

    lines = live_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    if len(lines) <= RECEIPTS_MAX_RECORDS:
        _emit("INFO", "receipts_skip", reason="within_cap", line_count=len(lines), cap=RECEIPTS_MAX_RECORDS)
        return 0

    archive_file = _archive_path(state_dir, "t0_receipts")
    if archive_file.exists():
        _emit("INFO", "receipts_skip", reason="archive_already_exists_today", archive=str(archive_file))
        return 0

    keep = lines[-RECEIPTS_MAX_RECORDS:]
    overflow = lines[: -RECEIPTS_MAX_RECORDS]

    _emit(
        "INFO",
        "receipts_rotating",
        live_path=str(live_file),
        archive_path=str(archive_file),
        archive_lines=len(overflow),
        keep_lines=len(keep),
        dry_run=dry_run,
    )

    if dry_run:
        return 0

    try:
        _atomic_write_bytes(archive_file, gzip.compress("".join(overflow).encode("utf-8")))
        _atomic_write_text(live_file, "".join(keep))
    except OSError as exc:
        _emit("ERROR", "receipts_io_error", error=str(exc))
        return 1

    _emit(
        "INFO",
        "receipts_done",
        archive=str(archive_file),
        archive_lines=len(overflow),
        live_lines=len(keep),
    )
    return 0


def compact_open_items_digest(state_dir: Path, *, dry_run: bool = False) -> int:
    """Evict open_items_digest.json entries where last_updated >30d. Returns 0 on success."""
    digest_file = state_dir / "open_items_digest.json"

    if not digest_file.exists():
        _emit("INFO", "open_items_digest_skip", reason="file_not_found", path=str(digest_file))
        return 0

    try:
        digest: dict = json.loads(digest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _emit("ERROR", "open_items_digest_parse_error", error=str(exc))
        return 1

    if not isinstance(digest, dict):
        _emit("ERROR", "open_items_digest_unexpected_schema", type=type(digest).__name__)
        return 1

    cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=OPEN_ITEMS_STALE_DAYS)

    def _is_stale(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        raw = entry.get("last_updated")
        if not raw:
            return False
        try:
            ts = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            return ts < cutoff
        except (ValueError, AttributeError):
            return False

    new_digest: dict = {}
    total_evicted = 0

    for key, value in digest.items():
        if isinstance(value, list):
            fresh = [e for e in value if not _is_stale(e)]
            evicted = len(value) - len(fresh)
            total_evicted += evicted
            new_digest[key] = fresh
        else:
            new_digest[key] = value

    if total_evicted == 0:
        _emit("INFO", "open_items_digest_skip", reason="no_stale_entries")
        return 0

    _emit("INFO", "open_items_digest_evicting", evicted=total_evicted, dry_run=dry_run)

    if dry_run:
        return 0

    try:
        content = json.dumps(new_digest, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(digest_file, content)
    except OSError as exc:
        _emit("ERROR", "open_items_digest_io_error", error=str(exc))
        return 1

    _emit("INFO", "open_items_digest_done", evicted=total_evicted)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="VNX state file compaction")
    parser.add_argument(
        "--mode",
        choices=["intelligence_archive", "receipts", "open_items_digest", "all"],
        default="all",
        help="Which compaction mode to run (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Describe what would be done without mutating any files",
    )
    args = parser.parse_args()

    data_dir = resolve_data_dir(__file__)
    state_dir = data_dir / "state"

    _emit("INFO", "compact_state_start", mode=args.mode, dry_run=args.dry_run, state_dir=str(state_dir))

    codes: list[int] = []

    if args.mode in ("intelligence_archive", "all"):
        codes.append(compact_intelligence_archive(state_dir, dry_run=args.dry_run))

    if args.mode in ("receipts", "all"):
        codes.append(compact_receipts(state_dir, dry_run=args.dry_run))

    if args.mode in ("open_items_digest", "all"):
        codes.append(compact_open_items_digest(state_dir, dry_run=args.dry_run))

    overall = max(codes) if codes else 0
    _emit("INFO", "compact_state_done", mode=args.mode, exit_code=overall)
    return overall


if __name__ == "__main__":
    sys.exit(main())
