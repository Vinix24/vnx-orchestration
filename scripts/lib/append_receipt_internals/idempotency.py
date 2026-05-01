"""Idempotency cache, lock-file paths, and key computation for receipt appends."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import (
    AppendReceiptError,
    AppendResult,
    EXIT_IO_ERROR,
    EXIT_LOCK_ERROR,
    facade,
)

IDEMPOTENCY_FIELDS = (
    "dispatch_id",
    "task_id",
    "pr_number",  # prevents review_gate_request fan-out collision per gate
    "gate",       # multiple gates per dispatch_id must not collide
    "terminal",
    "event_type",
    "event",
    "report_path",
    "source",
    "file",
    "trigger",
    "section",
)


def _resolve_receipts_file(receipts_file: Optional[str] = None) -> Path:
    if receipts_file:
        return Path(receipts_file).expanduser()
    paths = facade.ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _lock_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "append_receipt.lock"


def _cache_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "receipt_idempotency_recent.ndjson"


def _compute_idempotency_key(receipt: Dict[str, Any], event_name: str) -> str:
    digest_fields: Dict[str, Any] = {}

    for field in IDEMPOTENCY_FIELDS:
        value = receipt.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        digest_fields[field] = value

    if "event_type" not in digest_fields and "event" not in digest_fields:
        digest_fields["event_type"] = event_name

    if (
        "dispatch_id" not in digest_fields
        and "task_id" not in digest_fields
        and "report_path" not in digest_fields
    ):
        digest_fields["timestamp"] = receipt.get("timestamp")

    if not digest_fields:
        digest_fields = receipt

    payload = json.dumps(digest_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(cache_file: Path, min_epoch: float) -> List[Dict[str, Any]]:
    if not cache_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(parsed.get("ts", 0))
                key = str(parsed.get("key", "")).strip()
                if key and ts >= min_epoch:
                    entries.append({"ts": ts, "key": key})
    except OSError as exc:
        raise AppendReceiptError("cache_read_failed", EXIT_IO_ERROR, f"Failed to read idempotency cache: {exc}") from exc

    return entries


def _write_cache(cache_file: Path, entries: List[Dict[str, Any]], max_entries: int = 2048) -> None:
    entries = entries[-max_entries:]
    tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")

    try:
        with tmp_file.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
        os.replace(tmp_file, cache_file)
    except OSError as exc:
        raise AppendReceiptError("cache_write_failed", EXIT_IO_ERROR, f"Failed to write idempotency cache: {exc}") from exc
    finally:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


def _write_receipt_under_lock(
    receipt: Dict[str, Any],
    receipt_path: Path,
    cache_path: Path,
    idempotency_key: str,
    cache_window_seconds: int,
) -> AppendResult:
    """Acquire the append lock and either write the receipt or skip as duplicate."""
    lock_path = _lock_file_for(receipt_path)
    min_epoch = time.time() - max(1, int(cache_window_seconds))

    try:
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

            cache_entries = _load_cache(cache_path, min_epoch)
            recent_keys = {entry["key"] for entry in cache_entries}

            if idempotency_key in recent_keys:
                _write_cache(cache_path, cache_entries)
                return AppendResult(
                    status="duplicate",
                    receipts_file=receipt_path,
                    idempotency_key=idempotency_key,
                )

            try:
                with receipt_path.open("a", encoding="utf-8") as receipts_handle:
                    receipts_handle.write(json.dumps(receipt, separators=(",", ":"), sort_keys=False))
                    receipts_handle.write("\n")
            except OSError as exc:
                raise AppendReceiptError("receipt_write_failed", EXIT_IO_ERROR, f"Failed to append receipt: {exc}") from exc

            cache_entries.append({"ts": time.time(), "key": idempotency_key})
            _write_cache(cache_path, cache_entries)

            return AppendResult(
                status="appended",
                receipts_file=receipt_path,
                idempotency_key=idempotency_key,
            )
    except AppendReceiptError:
        raise
    except OSError as exc:
        raise AppendReceiptError("lock_failed", EXIT_LOCK_ERROR, f"Failed to acquire append lock: {exc}") from exc
