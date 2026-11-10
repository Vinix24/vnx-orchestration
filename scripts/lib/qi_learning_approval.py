#!/usr/bin/env python3
"""The approval record the learning loop's persist path requires (ADR-068, besluit 18): the read side.

``<state-dir>/learning_approvals/learn-<changes_sha256>.json`` binds one operator decision to one
change set. ``VNX_LEARNING_LOOP_APPROVED`` only selects the file; ``load_approval`` validates it and reads
``intelligence_usage.ndjson``. The ledger wins: a ``learning_persist`` line naming the proposal makes it used,
whatever its record says. The claim (under a lock, ledger first per ADR-005: that line, then ``consumed_at``)
lands in split 1b; ``write_record`` with the approve CLI in split 3.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Iterable, NoReturn

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


def _parsed(parse: Callable[[Any], Any], value: Any) -> Any:
    """``parse(value)``, or None when ``value`` does not parse."""
    try:
        return parse(value)
    except (TypeError, ValueError):
        return None


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
    if _claimed_in_log(state_dir, proposal_id):
        _refuse(f"approval {proposal_id} was already used: {USAGE_LOG_NAME} holds its learning_persist line")
    if not isinstance(record["approved_by"], str) or record["approved_by"] not in valid_approvers:
        _refuse(f"approved_by {record['approved_by']!r} is not one of {sorted(valid_approvers)}")
    approved_at = _parsed(datetime.fromisoformat, record["approved_at"])
    if not isinstance(record["reason"], str) or not record["reason"].strip() or approved_at is None:
        _refuse(f"approval record {path} has no reason or no ISO approved_at")
    return record


def _claimed_in_log(state_dir: Path, proposal_id: str) -> bool:
    """Whether the ledger holds a ``learning_persist`` line for ``proposal_id`` (a missing log holds none). Only a
    torn last append is skipped; any other line that is no JSON event, or a persist line naming no proposal, refuses."""
    log, claimed = Path(state_dir) / USAGE_LOG_NAME, False
    try:
        with open(log, "rb") as handle:
            for number, raw in enumerate(handle, 1):
                event = _parsed(json.loads, raw)
                persist = isinstance(event, dict) and event.get("event_type") == "learning_persist"
                if (not isinstance(event, dict) and raw.endswith(b"\n")
                        or persist and not _PROPOSAL_ID.fullmatch(str(event.get("proposal_id")))):
                    _refuse(f"{log} line {number} is no JSON event or a persist line naming no proposal: "
                            "the log is not trustworthy")
                claimed = claimed or persist and event["proposal_id"] == proposal_id
    except FileNotFoundError:
        return False
    except OSError as exc:
        _refuse(f"{log} is unreadable ({exc}), so whether {proposal_id} was used is unknown")
    return claimed
