#!/usr/bin/env python3
"""Shadow writer for terminal_state.json."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import fcntl

TERMINAL_STATE_FILENAME = "terminal_state.json"
TERMINAL_STATE_LOCK = ".terminal_state.lock"
SCHEMA_VERSION = 1


class TerminalStateValidationError(ValueError):
    """Raised when terminal_state.json structure is invalid."""


@dataclass
class TerminalUpdate:
    terminal_id: str
    status: str
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    last_activity: Optional[str] = None
    clear_claim: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_lease_expires(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _empty_document() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "terminals": {}}


def _validate_iso_or_none(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise TerminalStateValidationError(f"{field} must be a non-empty ISO datetime string or null")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TerminalStateValidationError(f"{field} is not a valid ISO datetime: {value}") from exc


def validate_terminal_record(record: Dict[str, Any]) -> None:
    required = {
        "terminal_id": str,
        "status": str,
        "version": int,
    }
    for field, field_type in required.items():
        if field not in record:
            raise TerminalStateValidationError(f"Missing required field: {field}")
        if not isinstance(record[field], field_type):
            raise TerminalStateValidationError(f"Invalid type for {field}: expected {field_type.__name__}")

    if record["version"] < 1:
        raise TerminalStateValidationError("version must be >= 1")

    optional_fields = ["claimed_by", "claimed_at", "lease_expires_at", "last_activity"]
    for field in optional_fields:
        if field in record and record[field] is not None and not isinstance(record[field], str):
            raise TerminalStateValidationError(f"{field} must be string or null")

    _validate_iso_or_none(record.get("claimed_at"), "claimed_at")
    _validate_iso_or_none(record.get("lease_expires_at"), "lease_expires_at")
    _validate_iso_or_none(record.get("last_activity"), "last_activity")


def validate_terminal_state_document(document: Dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise TerminalStateValidationError("Document must be an object")
    if "schema_version" not in document:
        raise TerminalStateValidationError("Missing schema_version")
    if not isinstance(document["schema_version"], int):
        raise TerminalStateValidationError("schema_version must be an integer")
    if "terminals" not in document or not isinstance(document["terminals"], dict):
        raise TerminalStateValidationError("terminals must be an object")
    for record in document["terminals"].values():
        if not isinstance(record, dict):
            raise TerminalStateValidationError("terminal record must be an object")
        validate_terminal_record(record)


def _load_document(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _empty_document()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_terminal_state_document(data)
        return data
    except Exception:
        # Shadow mode safety: if file is corrupt, recover by resetting structure.
        return _empty_document()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    validate_terminal_state_document(payload)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _is_lease_expired(lease_expires_at: str | None) -> bool:
    """Check if a lease timestamp is in the past."""
    if not lease_expires_at:
        return False
    try:
        expires = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
        return expires <= datetime.now(timezone.utc)
    except (ValueError, AttributeError):
        return False


def _gc_expired_leases(terminals: Dict[str, Any]) -> int:
    """Clear claim fields on all terminals with expired leases. Returns count of cleaned entries."""
    cleaned = 0
    for record in terminals.values():
        if not isinstance(record, dict):
            continue
        claimed_by = record.get("claimed_by")
        lease_expires_at = record.get("lease_expires_at")
        if claimed_by and _is_lease_expired(lease_expires_at):
            record["claimed_by"] = None
            record["claimed_at"] = None
            record["lease_expires_at"] = None
            record["status"] = "idle"
            cleaned += 1
    return cleaned


def update_terminal_state(state_dir: str | Path, update: TerminalUpdate) -> Dict[str, Any]:
    if not update.terminal_id:
        raise TerminalStateValidationError("terminal_id is required")
    if not update.status:
        raise TerminalStateValidationError("status is required")

    state_root = Path(state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    state_file = state_root / TERMINAL_STATE_FILENAME
    lock_path = state_root / TERMINAL_STATE_LOCK

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        document = _load_document(state_file)
        terminals = document.setdefault("terminals", {})

        # Garbage-collect expired leases on every write (cheap: iterates 3-4 terminals)
        _gc_expired_leases(terminals)

        existing = terminals.get(update.terminal_id, {})
        version = int(existing.get("version", 0)) + 1

        claimed_by = existing.get("claimed_by")
        claimed_at = existing.get("claimed_at")
        lease_expires_at = existing.get("lease_expires_at")
        last_activity = existing.get("last_activity")

        if update.clear_claim:
            claimed_by = None
            claimed_at = None
            lease_expires_at = None
        # clear_claim is authoritative: completion/cleanup updates must never
        # re-introduce claim fields in the same write.
        if not update.clear_claim and update.claimed_by is not None:
            claimed_by = update.claimed_by or None
        if not update.clear_claim and update.claimed_at is not None:
            claimed_at = update.claimed_at or None
        if not update.clear_claim and update.lease_expires_at is not None:
            lease_expires_at = update.lease_expires_at or None
        if update.last_activity is not None:
            last_activity = update.last_activity or None

        record = {
            "terminal_id": update.terminal_id,
            "status": update.status,
            "claimed_by": claimed_by,
            "claimed_at": claimed_at,
            "lease_expires_at": lease_expires_at,
            "last_activity": last_activity or _now_iso(),
            "version": version,
        }
        validate_terminal_record(record)
        terminals[update.terminal_id] = record
        document["schema_version"] = SCHEMA_VERSION

        _atomic_write_json(state_file, document)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    return record
