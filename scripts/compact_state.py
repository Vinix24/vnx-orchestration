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


def _stage_bytes(directory: Path, content: bytes) -> Path:
    """Write content to a temp file in directory; return path without committing to final name.

    Caller must os.replace() the returned path to its final destination, or unlink() it on failure.
    Using a staging temp instead of writing directly to the archive path ensures the archive
    path only appears after a successful live-file write (see Finding 2).
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_archive_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return Path(tmp)


def _cia_check_skip(live_file: Path, state_dir: Path) -> tuple[Path | None, str | None]:
    """Check pre-conditions for intelligence archive rotation.

    Returns (archive_file, None) to proceed or (None, skip_reason) to skip.
    A staging temp (.tmp_archive_*) does not count as an existing archive.
    """
    if not live_file.exists():
        _emit("INFO", "intelligence_archive_skip", reason="file_not_found", path=str(live_file))
        return None, "file_not_found"
    size_bytes = live_file.stat().st_size
    min_bytes = INTELLIGENCE_ARCHIVE_MIN_MB * 1024 * 1024
    if size_bytes < min_bytes:
        _emit(
            "INFO", "intelligence_archive_skip",
            reason="below_threshold_mb",
            size_mb=round(size_bytes / 1024 / 1024, 2),
            threshold_mb=INTELLIGENCE_ARCHIVE_MIN_MB,
        )
        return None, "below_threshold"
    archive_file = _archive_path(state_dir, "t0_intelligence_archive")
    if archive_file.exists():
        _emit("INFO", "intelligence_archive_skip", reason="archive_already_exists_today",
              archive=str(archive_file))
        return None, "already_exists"
    return archive_file, None


def _cia_partition_lines(lines: list[str], cutoff_ts: float) -> tuple[list[str], list[str]]:
    """Split NDJSON lines into keep (recent) and archive (old) lists by cutoff timestamp."""
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
    return keep, archive


def _cia_write_atomic(live_file: Path, archive_file: Path, keep: list[str], archive: list[str]) -> int:
    """Two-phase write: stage archive → rewrite live → promote archive. Returns 0 on success.

    A partial failure leaves no committed archive path, allowing safe retry.
    """
    archive_tmp: Path | None = None
    try:
        archive_tmp = _stage_bytes(
            archive_file.parent, gzip.compress("".join(archive).encode("utf-8"))
        )
        _atomic_write_text(live_file, "".join(keep))
        os.replace(archive_tmp, archive_file)
        archive_tmp = None
    except OSError as exc:
        _emit("ERROR", "intelligence_archive_io_error", error=str(exc))
        return 1
    finally:
        if archive_tmp is not None:
            try:
                os.unlink(archive_tmp)
            except OSError:
                pass
    return 0


def compact_intelligence_archive(state_dir: Path, *, dry_run: bool = False) -> int:
    """Rotate t0_intelligence_archive.ndjson. Returns 0 on success, non-zero on error."""
    live_file = state_dir / "t0_intelligence_archive.ndjson"

    archive_file, skip_reason = _cia_check_skip(live_file, state_dir)
    if skip_reason is not None:
        return 0

    cutoff_ts = time.time() - INTELLIGENCE_ARCHIVE_KEEP_DAYS * 86400
    lines = live_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    keep, archive = _cia_partition_lines(lines, cutoff_ts)

    if not archive:
        _emit("INFO", "intelligence_archive_skip", reason="all_records_within_7d", total_lines=len(lines))
        return 0

    _emit(
        "INFO", "intelligence_archive_rotating",
        live_path=str(live_file), archive_path=str(archive_file),
        archive_lines=len(archive), keep_lines=len(keep), dry_run=dry_run,
    )

    if dry_run:
        return 0

    rc = _cia_write_atomic(live_file, archive_file, keep, archive)
    if rc != 0:
        return rc

    _emit(
        "INFO", "intelligence_archive_done",
        archive=str(archive_file), archive_lines=len(archive), live_lines=len(keep),
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
    # Check for an already-committed archive; staging temps do not count (partial-failure remnants).
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

    # Two-phase write: stage archive content to a temp path so the real archive path
    # only appears after the live-file rewrite succeeds.  A partial failure leaves no
    # committed archive, allowing safe retry.
    archive_tmp: Path | None = None
    try:
        archive_tmp = _stage_bytes(
            archive_file.parent, gzip.compress("".join(overflow).encode("utf-8"))
        )
        _atomic_write_text(live_file, "".join(keep))
        os.replace(archive_tmp, archive_file)
        archive_tmp = None
    except OSError as exc:
        _emit("ERROR", "receipts_io_error", error=str(exc))
        return 1
    finally:
        if archive_tmp is not None:
            try:
                os.unlink(archive_tmp)
            except OSError:
                pass

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

    # Digest-level timestamp used as fallback when a list entry carries no per-entry timestamp.
    # recent_closures entries only have {id, title, closed_reason, closed_at}; older digests
    # written before closed_at existed have no timestamp at all.  Using digest_generated ensures
    # those entries are still evicted once the whole digest is stale.
    digest_ts_fallback: str | None = digest.get("digest_generated") or digest.get("last_updated")

    def _is_stale(entry: object, *, fallback_ts: str | None = None) -> bool:
        if not isinstance(entry, dict):
            return False
        # Check common timestamp fields; closed_at and updated_at are used by recent_closures.
        raw = (
            entry.get("last_updated")
            or entry.get("closed_at")
            or entry.get("updated_at")
            or fallback_ts
        )
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
            fresh = [e for e in value if not _is_stale(e, fallback_ts=digest_ts_fallback)]
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
