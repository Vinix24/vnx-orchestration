"""Outcome identity: a durable identity for WHAT happened, independent of
HOW or via which route it was booked (ADR-038).

Two booking routes write completion receipts for the same dispatch outcome:
the lane itself (``governance_emit`` / ``envelope_govern`` / ``dispatch_govern``
/ ``provider_dispatch``) right after the work finishes, and
``report_to_receipt_converter`` when it later turns a report in
``unified_reports/`` into a receipt. Both call ``append_receipt_payload``.
The existing idempotency key (``idempotency.IDEMPOTENCY_FIELDS``) includes
route-identifying fields — ``source``, ``report_path``, ``file``, ``trigger``,
``section`` — that legitimately differ between the two routes for the exact
same outcome, so the same outcome gets two different keys and lands as two
ledger lines. The existing dedup cache is also only a
``cache_window_seconds`` (default 300s) window, so a re-booking after a
longer gap (a backfill, a re-processed report) is invisible to it regardless
of key.

This module defines the identity of the OUTCOME itself — dispatch_id,
event_type, status, commit_sha, gate, pr_number — deliberately excluding
every route-identifying field, and a durable on-disk index
(``receipt_outcome_index.json``, sibling to the ledger file) so the dedup
check spans the whole ledger rather than a short cache window. See
``docs/governance/decisions/ADR-038-receipt-outcome-identity.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Fields that identify the OUTCOME. Deliberately NOT source/report_path/
# file/trigger/section/timestamp — those identify the booking ROUTE, not the
# outcome, and are exactly the fields that differ between the two routes for
# one real-world event (see module docstring).
OUTCOME_ID_FIELDS = ("dispatch_id", "event_type", "status", "commit_sha", "gate", "pr_number")

# Same identity, minus status: the "family" of outcomes for one
# dispatch/event/gate/pr/commit. A new status within the same family is a
# correction (ADR-005 — corrections are new lines that reference the
# corrected event by ID, never an in-place edit), not a duplicate.
CORRECTION_KEY_FIELDS = ("dispatch_id", "event_type", "commit_sha", "gate", "pr_number")

OUTCOME_INDEX_FILENAME = "receipt_outcome_index.json"
OUTCOME_INDEX_VERSION = 1


def _normalize_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _resolve_event_name(receipt: Dict[str, Any]) -> str:
    """``event_type`` with the legacy ``event`` alias as fallback.

    Independent re-derivation (not a call into ``validation._resolve_event_name``)
    so this module has no dependency on the schema-version-aware resolver —
    by the time a receipt reaches the append lock it already carries whichever
    of the two keys its schema version uses; either is a stable identity input.
    """
    return _normalize_field(receipt.get("event_type") or receipt.get("event"))


def _canonical_payload(receipt: Dict[str, Any], fields: tuple) -> str:
    values: Dict[str, str] = {}
    for field_name in fields:
        if field_name == "event_type":
            values[field_name] = _resolve_event_name(receipt)
        else:
            values[field_name] = _normalize_field(receipt.get(field_name))
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def compute_outcome_id(receipt: Dict[str, Any]) -> str:
    """Deterministic identity hash over ``OUTCOME_ID_FIELDS``.

    Two receipts for the same outcome booked via different routes (different
    source/report_path/terminal/task_id) collapse to the same outcome_id.
    """
    payload = _canonical_payload(receipt, OUTCOME_ID_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_correction_key(receipt: Dict[str, Any]) -> str:
    """Deterministic hash over ``CORRECTION_KEY_FIELDS`` (outcome identity
    minus status) — the family a correction is scoped to."""
    payload = _canonical_payload(receipt, CORRECTION_KEY_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_outcome_identity(receipt: Dict[str, Any]) -> bool:
    """True when the receipt carries a real ``dispatch_id``.

    Durable cross-window dedup is scoped to dispatch outcomes — the concrete
    two-route double-booking problem this module exists to fix. A receipt
    with no dispatch_id (e.g. some state_mutation/test events) would collapse
    onto the SAME outcome_id as every other dispatch_id-less receipt (all six
    fields empty) and silently swallow genuinely distinct events; excluding
    them from enforcement is deliberate, not an oversight.  ``outcome_id`` is
    still computed and stamped on such a receipt (every receipt gets one) —
    only the durable-index duplicate/correction check is skipped.
    """
    return bool(_normalize_field(receipt.get("dispatch_id")))


def _outcome_index_path_for(receipts_path: Path) -> Path:
    return receipts_path.parent / OUTCOME_INDEX_FILENAME


def _empty_index() -> Dict[str, Any]:
    return {"version": OUTCOME_INDEX_VERSION, "outcomes": {}, "correction_index": {}}


def load_outcome_index(index_path: Path) -> Dict[str, Any]:
    """Load the durable outcome index, or an empty one if absent/corrupt.

    A corrupt/unreadable index fails open (returns empty) rather than
    raising — callers must not let a projection-index problem block the
    durable receipt write, matching this append path's existing posture
    toward the short-window idempotency cache (``idempotency._load_cache``).
    """
    if not index_path.exists():
        return _empty_index()
    try:
        with index_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("outcome index unreadable, treating as empty (fail-open): %s", exc)
        return _empty_index()
    if not isinstance(data, dict):
        return _empty_index()
    data.setdefault("version", OUTCOME_INDEX_VERSION)
    data.setdefault("outcomes", {})
    data.setdefault("correction_index", {})
    return data


def write_outcome_index(index_path: Path, data: Dict[str, Any]) -> None:
    """Durable atomic write (tmp file + os.replace), mirroring
    ``idempotency._write_cache``'s convention."""
    tmp_path = index_path.with_name(f"{index_path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp_path, index_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


@dataclass(frozen=True)
class OutcomeBooking:
    outcome_id: str
    supersedes: Optional[str]
    is_duplicate: bool


def resolve_outcome_booking(receipt: Dict[str, Any], index_data: Dict[str, Any]) -> OutcomeBooking:
    """Pure decision: given a receipt and the CURRENT index state, decide
    duplicate / correction / fresh outcome. Never mutates ``index_data`` —
    the caller commits via ``record_outcome`` only after the receipt has
    actually been written (mirrors the append-then-commit ordering the
    idempotency cache and receipt_finalize's pre_write_hook already use)."""
    outcome_id = compute_outcome_id(receipt)
    if outcome_id in index_data["outcomes"]:
        return OutcomeBooking(outcome_id=outcome_id, supersedes=None, is_duplicate=True)

    correction_key = compute_correction_key(receipt)
    prior_outcome_id = index_data["correction_index"].get(correction_key)
    supersedes = prior_outcome_id if prior_outcome_id and prior_outcome_id != outcome_id else None
    return OutcomeBooking(outcome_id=outcome_id, supersedes=supersedes, is_duplicate=False)


def record_outcome(receipt: Dict[str, Any], index_data: Dict[str, Any], booking: OutcomeBooking) -> None:
    """Mutate ``index_data`` in place to record a booked (non-duplicate) outcome."""
    correction_key = compute_correction_key(receipt)
    index_data["outcomes"][booking.outcome_id] = int(time.time())
    index_data["correction_index"][correction_key] = booking.outcome_id


@dataclass
class RebuildStats:
    total_lines: int = 0
    malformed_lines: int = 0
    considered: int = 0
    booked: int = 0
    duplicates: int = 0
    corrections: int = 0


def rebuild_outcome_index(receipts_path: Path, *, write: bool = True) -> RebuildStats:
    """Replay ``receipts_path`` from scratch and (re)derive the outcome index.

    ADR-005: the index is a projection over the ledger and MUST be
    rebuildable from it. Also serves as the dry-run measurement tool: with
    ``write=False`` this never touches disk (no index file is created or
    updated) and simply reports what the index WOULD have caught, which is
    exactly the "how many double-bookings does this catch" measurement this
    dispatch is required to take before and after the change.
    """
    stats = RebuildStats()
    index_data = _empty_index()

    if receipts_path.exists():
        with receipts_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                stats.total_lines += 1
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError:
                    stats.malformed_lines += 1
                    continue
                if not isinstance(receipt, dict) or not has_outcome_identity(receipt):
                    continue
                stats.considered += 1
                booking = resolve_outcome_booking(receipt, index_data)
                if booking.is_duplicate:
                    stats.duplicates += 1
                    continue
                if booking.supersedes:
                    stats.corrections += 1
                record_outcome(receipt, index_data, booking)
                stats.booked += 1

    if write:
        write_outcome_index(_outcome_index_path_for(receipts_path), index_data)

    return stats
