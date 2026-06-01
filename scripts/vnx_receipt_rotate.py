#!/usr/bin/env python3
"""Receipt-ledger rotation for t0_receipts.ndjson.

Rotates the live ledger when it exceeds a size threshold. Rotation is atomic
(rename, not copy-truncate) and preserves hash-chain continuity by writing a
`ledger_rotation` sentinel as the first entry of the new live file.

The sentinel carries `prev_hash` = hash of the last archived entry, so a
full-history verifier can walk archives in chronological order then the live
file and validate the chain end-to-end.

Usage:
    python3 scripts/vnx_receipt_rotate.py [--check] [--force]
    python3 scripts/vnx_receipt_rotate.py --receipts-file /path/to/t0_receipts.ndjson

Environment variables:
    VNX_RECEIPT_ROTATE_MAX_MB   Rotation threshold in MB (default: 50)
    VNX_STATE_DIR               State directory (resolved via vnx_paths if unset)
    VNX_DATA_DIR                Data directory (resolved via vnx_paths if unset)

Full-history verification across rotation boundary:
    Load archives in ascending filename-timestamp order, then the live file.
    The live file's first entry (ledger_rotation sentinel) has prev_hash =
    hash of the last entry in the last archive. Pass each file's entries
    through ndjson_hash_chain.verify_chain with the expected_prev carried
    across file boundaries via the sentinel's prev_hash field.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from ndjson_hash_chain import GENESIS_HASH, compute_entry_hash

DEFAULT_MAX_MB = 50.0
_ARCHIVE_SUBDIR = "archive"
_LOCK_FILENAME = "append_receipt.lock"
_ROTATION_EVENT_TYPE = "ledger_rotation"


def _resolve_receipts_file(receipts_file: Optional[str] = None) -> Path:
    if receipts_file:
        return Path(receipts_file).expanduser().resolve()
    try:
        from vnx_paths import ensure_env
        paths = ensure_env()
        return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
    except Exception:
        state_dir = os.environ.get("VNX_STATE_DIR", "")
        if not state_dir:
            raise RuntimeError("Cannot resolve receipts file: VNX_STATE_DIR not set")
        return Path(state_dir) / "t0_receipts.ndjson"


def _resolve_archive_dir(receipts_path: Path, archive_dir: Optional[str] = None) -> Path:
    if archive_dir:
        return Path(archive_dir).expanduser().resolve()
    return receipts_path.parent / _ARCHIVE_SUBDIR


def _read_last_entry(receipts_path: Path) -> Optional[dict]:
    if not receipts_path.exists() or receipts_path.stat().st_size == 0:
        return None
    try:
        with receipts_path.open("rb") as f:
            try:
                f.seek(-2, 2)
                while f.read(1) != b"\n":
                    f.seek(-2, 1)
            except OSError:
                f.seek(0)
            last_line = f.readline().decode("utf-8").strip()
        if not last_line:
            return None
        return json.loads(last_line)
    except (OSError, json.JSONDecodeError):
        return None


def _count_lines(receipts_path: Path) -> int:
    if not receipts_path.exists():
        return 0
    count = 0
    with receipts_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            count += chunk.count(b"\n")
    return count


def _make_archive_name(ts: str, archive_dir: Path) -> str:
    safe_ts = ts.replace(":", "-").replace("+", "").replace("Z", "")
    candidate = f"t0_receipts-{safe_ts}.ndjson"
    if not (archive_dir / candidate).exists():
        return candidate
    # Collision guard: append a counter suffix.
    counter = 1
    while True:
        candidate = f"t0_receipts-{safe_ts}-{counter}.ndjson"
        if not (archive_dir / candidate).exists():
            return candidate
        counter += 1


def _write_rotation_sentinel(new_receipts_path: Path, prev_hash: str, archive_path: Path, archived_lines: int) -> None:
    sentinel = {
        "event_type": _ROTATION_EVENT_TYPE,
        "prev_hash": prev_hash,
        "archive_path": str(archive_path),
        "archived_lines": archived_lines,
        "rotated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "vnx_receipt_rotate",
    }
    new_receipts_path.parent.mkdir(parents=True, exist_ok=True)
    with new_receipts_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(sentinel, separators=(",", ":"), sort_keys=False) + "\n")


def rotate(
    receipts_file: Optional[str] = None,
    archive_dir: Optional[str] = None,
    max_mb: float = DEFAULT_MAX_MB,
    force: bool = False,
) -> dict:
    """Rotate the receipt ledger if it exceeds max_mb.

    Returns a result dict with keys:
        rotated (bool), reason (str), archive_path (str|None),
        archived_lines (int), size_mb (float)
    """
    receipts_path = _resolve_receipts_file(receipts_file)
    archive_base = _resolve_archive_dir(receipts_path, archive_dir)

    if not receipts_path.exists():
        return {
            "rotated": False,
            "reason": "receipts_file_not_found",
            "archive_path": None,
            "archived_lines": 0,
            "size_mb": 0.0,
        }

    size_bytes = receipts_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)

    if not force and size_mb < max_mb:
        return {
            "rotated": False,
            "reason": "below_threshold",
            "archive_path": None,
            "archived_lines": 0,
            "size_mb": size_mb,
        }

    lock_path = receipts_path.parent / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

        # Re-read size under lock to avoid TOCTOU.
        if not force:
            current_size = receipts_path.stat().st_size if receipts_path.exists() else 0
            current_mb = current_size / (1024 * 1024)
            if current_mb < max_mb:
                return {
                    "rotated": False,
                    "reason": "below_threshold_under_lock",
                    "archive_path": None,
                    "archived_lines": 0,
                    "size_mb": current_mb,
                }

        last_entry = _read_last_entry(receipts_path)
        if last_entry is not None:
            prev_hash = compute_entry_hash(last_entry)
        else:
            prev_hash = GENESIS_HASH

        archived_lines = _count_lines(receipts_path)

        ts_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_base.mkdir(parents=True, exist_ok=True)
        archive_path = archive_base / _make_archive_name(ts_str, archive_base)

        # Atomic rename — safe as long as archive is on the same filesystem.
        os.rename(str(receipts_path), str(archive_path))

        # Write the new live file with rotation sentinel as first entry.
        _write_rotation_sentinel(receipts_path, prev_hash, archive_path, archived_lines)

    return {
        "rotated": True,
        "reason": "threshold_exceeded" if not force else "forced",
        "archive_path": str(archive_path),
        "archived_lines": archived_lines,
        "size_mb": size_mb,
    }


def check(receipts_file: Optional[str] = None, max_mb: float = DEFAULT_MAX_MB) -> dict:
    """Report current ledger size without rotating."""
    try:
        receipts_path = _resolve_receipts_file(receipts_file)
    except RuntimeError as exc:
        return {"error": str(exc), "size_mb": 0.0, "would_rotate": False}

    if not receipts_path.exists():
        return {"receipts_file": str(receipts_path), "size_mb": 0.0, "would_rotate": False}

    size_bytes = receipts_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    lines = _count_lines(receipts_path)
    return {
        "receipts_file": str(receipts_path),
        "size_mb": round(size_mb, 3),
        "size_bytes": size_bytes,
        "line_count": lines,
        "max_mb": max_mb,
        "would_rotate": size_mb >= max_mb,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rotate t0_receipts.ndjson when it exceeds a size threshold")
    parser.add_argument("--receipts-file", default=None, help="Override receipts file path")
    parser.add_argument("--archive-dir", default=None, help="Archive directory (default: sibling archive/ subdir)")
    parser.add_argument(
        "--max-mb",
        type=float,
        default=float(os.environ.get("VNX_RECEIPT_ROTATE_MAX_MB", DEFAULT_MAX_MB)),
        help=f"Threshold in MB (env: VNX_RECEIPT_ROTATE_MAX_MB, default: {DEFAULT_MAX_MB})",
    )
    parser.add_argument("--check", action="store_true", help="Report size without rotating")
    parser.add_argument("--force", action="store_true", help="Rotate regardless of threshold")
    args = parser.parse_args(argv)

    if args.check:
        result = check(receipts_file=args.receipts_file, max_mb=args.max_mb)
        print(json.dumps(result, indent=2))
        return 0

    result = rotate(
        receipts_file=args.receipts_file,
        archive_dir=args.archive_dir,
        max_mb=args.max_mb,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("rotated") or result.get("reason") in ("below_threshold", "below_threshold_under_lock") else 1


if __name__ == "__main__":
    raise SystemExit(main())
