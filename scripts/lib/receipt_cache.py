#!/usr/bin/env python3
"""receipt_cache.py — Idempotency key, cache I/O, and completion event classifiers.

Extracted from append_receipt.py to keep the main module under 500 lines.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

EXIT_OK = 0
EXIT_INVALID_INPUT = 10
EXIT_VALIDATION_ERROR = 11
EXIT_IO_ERROR = 12
EXIT_LOCK_ERROR = 13
EXIT_UNEXPECTED_ERROR = 20

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


class AppendReceiptError(RuntimeError):
    def __init__(self, code: str, exit_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message


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

    # For receipts without stable identity fields, include timestamp to avoid
    # collapsing distinct events in the short dedupe window.
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


def _is_completion_event(receipt: Dict[str, Any]) -> bool:
    """Check if receipt is a completion event."""
    event_type = receipt.get("event_type") or receipt.get("event") or ""
    return event_type in (
        "task_complete",
        "task_completed",
        "completion",
        "complete",
        "subprocess_completion",
    )


def _is_subprocess_intermediate_completion(receipt: Dict[str, Any]) -> bool:
    """True for the intermediate subprocess-adapter completion receipt.

    These receipts are appended when the subprocess exits but BEFORE the
    real report has been extracted (subprocess_adapter only drops an async
    trigger file at that point). They typically lack ``report_path`` and a
    git diff against HEAD will report no changed files, so generating a
    quality advisory or persisting CQS would overwrite ``dispatch_metadata``
    with synthetic "No changed files detected" data and corrupt the row
    permanently if the downstream report-driven enrichment is delayed or
    fails.

    The session/provenance/snapshot enrichment is still safe and desirable
    for these receipts (e.g. instruction_sha256 surfacing).
    """
    event_type = receipt.get("event_type") or receipt.get("event") or ""
    return event_type == "subprocess_completion"
