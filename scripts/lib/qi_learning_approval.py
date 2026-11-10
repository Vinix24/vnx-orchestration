#!/usr/bin/env python3
"""The approval record the learning loop's persist path requires (ADR-068, besluit 18).

``<state-dir>/learning_approvals/learn-<changes_sha256>.json`` binds one operator decision to one
change set. ``VNX_LEARNING_LOOP_APPROVED`` only selects the file; the hash is recomputed from the rows
about to be written. Used once: under a lock, before the first DB write, ``consumed_at`` is set and a
``learning_persist`` receipt with the record hash goes to ``intelligence_usage.ndjson``.
Tamper-evident, not tamper-proof: ``write_record`` logs a ``learning_approval`` event with the record
hash first, so a receipt whose record hash no approval event carries points at a forged record.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, List, NoReturn

from atomic_io import atomic_write_json, slot_lock
from vnx_paths import refuse_real_central_store_write_under_test_runner

APPROVED_ENV = "VNX_LEARNING_LOOP_APPROVED"
RECORD_DIRNAME = "learning_approvals"
USAGE_LOG_NAME = "intelligence_usage.ndjson"
RECORD_FIELDS = ("proposal_id", "changes_sha256", "approved_by", "approved_at", "reason", "consumed_at")
_PROPOSAL_ID = re.compile(r"learn-([0-9a-f]{64})")
# The write instant, not learned content: a recomputation at persist time
# carries another clock value than the preview the operator approved.
_WRITE_CLOCK_FIELDS = frozenset({"first_seen", "last_used", "last_seen", "valid_from"})


class LearningApprovalRefused(RuntimeError):
    """Raised before any DB write: the reason is in the message."""


def _refuse(reason: str) -> NoReturn:
    raise LearningApprovalRefused(f"{reason}; nothing was written")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def preview(changes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The rows as the hash sees them (clock fields dropped, order fixed), their hash and the id to approve."""
    rows = sorted(({k: v for k, v in row.items() if k not in _WRITE_CLOCK_FIELDS} for row in changes), key=_canonical)
    digest = _sha256(rows)
    return {"proposal_id": f"learn-{digest}", "changes_sha256": digest, "changes": rows}


def record_path(state_dir: Path, proposal_id: str) -> Path:
    return Path(state_dir) / RECORD_DIRNAME / f"{proposal_id}.json"


def _digest_of(proposal_id: Any) -> str:
    match = _PROPOSAL_ID.fullmatch(proposal_id) if isinstance(proposal_id, str) else None
    if match is None:
        _refuse(f"{proposal_id!r} is no proposal id (learn-<64 hex> from the shadow preview)")
    return match.group(1)


def _is_iso(value: Any) -> bool:
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def load_approval(state_dir: Path, valid_approvers: Collection[str]) -> Dict[str, Any]:
    """The unused record ``VNX_LEARNING_LOOP_APPROVED`` selects, or a refusal naming why. Reads only."""
    proposal_id = os.environ.get(APPROVED_ENV)
    if not proposal_id:
        _refuse(f"{APPROVED_ENV} is not set: persist needs an approval record (ADR-068 besluit 18)")
    digest = _digest_of(proposal_id)
    path = record_path(state_dir, proposal_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _refuse(f"no approval record at {path}")
    except (OSError, ValueError) as exc:
        _refuse(f"approval record {path} is unreadable or no JSON ({exc})")
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
        _refuse(f"approval record {path} does not carry exactly the fields {list(RECORD_FIELDS)}")
    if record["proposal_id"] != proposal_id or record["changes_sha256"] != digest:
        _refuse(f"approval record {path} belongs to another proposal")
    if record["consumed_at"] is not None:
        _refuse(f"approval record {path} was already used ({record['consumed_at']!r})")
    if not isinstance(record["approved_by"], str) or record["approved_by"] not in valid_approvers:
        _refuse(f"approved_by {record['approved_by']!r} is not one of {sorted(valid_approvers)}")
    if not isinstance(record["reason"], str) or not record["reason"].strip() or not _is_iso(record["approved_at"]):
        _refuse(f"approval record {path} has no reason or no ISO approved_at")
    return record


def _guard_writes(state_dir: Path, path: Path) -> None:
    for target in (path, Path(state_dir) / USAGE_LOG_NAME):
        refuse_real_central_store_write_under_test_runner(target)


def _append_event(state_dir: Path, event: Dict[str, Any]) -> None:
    with open(Path(state_dir) / USAGE_LOG_NAME, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(_canonical(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def claim_approval(state_dir: Path, valid_approvers: Collection[str], changes: List[Dict[str, Any]]) -> str:
    """Bind the selected record to ``changes`` and use it up before the first DB write; returns the
    record sha256. Re-read under the lock: of two concurrent persists on one record only one gets through."""
    record = load_approval(state_dir, valid_approvers)
    try:
        actual = preview(changes)["changes_sha256"]
    except (TypeError, ValueError) as exc:
        _refuse(f"the rows to write are no canonical JSON ({exc})")
    if actual != record["changes_sha256"]:
        _refuse(f"the rows to write hash to {actual}, the approval is for {record['changes_sha256']}")
    path = record_path(state_dir, record["proposal_id"])
    _guard_writes(state_dir, path)
    with slot_lock(path):
        if load_approval(state_dir, valid_approvers) != record:
            _refuse(f"approval record {path} changed while this persist read it")
        record_sha256, consumed_at = _sha256(record), datetime.now(timezone.utc).isoformat()
        try:
            atomic_write_json(path, dict(record, consumed_at=consumed_at))
            _append_event(state_dir, {"event_type": "learning_persist", "timestamp": consumed_at,
                                      "proposal_id": record["proposal_id"], "changes_sha256": actual,
                                      "approval_record_sha256": record_sha256,
                                      "approved_by": record["approved_by"], "rows": len(changes)})
        except OSError as exc:
            raise LearningApprovalRefused(f"approval record {path} not used or its receipt not written ({exc}); "
                                          "no row was written and the record may count as used") from exc
    return record_sha256


def write_record(state_dir: Path, proposal_id: str, approved_by: str, reason: str,
                 valid_approvers: Collection[str]) -> Path:
    """Create an unused record, its ``learning_approval`` event first. Never overwrites a record."""
    digest = _digest_of(proposal_id)
    if not isinstance(approved_by, str) or approved_by not in valid_approvers:
        _refuse(f"approved_by {approved_by!r} is not one of {sorted(valid_approvers)}")
    if not isinstance(reason, str) or not reason.strip():
        _refuse("an approval needs a non-empty --reason")
    path = record_path(state_dir, proposal_id)
    _guard_writes(state_dir, path)
    record = {"proposal_id": proposal_id, "changes_sha256": digest, "approved_by": approved_by,
              "approved_at": datetime.now(timezone.utc).isoformat(), "reason": reason.strip(), "consumed_at": None}
    with slot_lock(path):
        if path.exists() or path.is_symlink():
            _refuse(f"{path} already exists; an approval record is never overwritten")
        _append_event(state_dir, {"event_type": "learning_approval", "timestamp": record["approved_at"],
                                  "proposal_id": proposal_id, "changes_sha256": digest,
                                  "approval_record_sha256": _sha256(record), "approved_by": approved_by})
        atomic_write_json(path, record)
    return path
