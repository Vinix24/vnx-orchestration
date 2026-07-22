"""Receipt validation and completion-event detection."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .common import (
    AppendReceiptError,
    EXIT_VALIDATION_ERROR,
    facade,
    get_facade_module,
)
from .warning_destination import (
    DROP_REASON_ALLOWLIST,
    LEGAL_DESTINATIONS,
    LEGAL_SEVERITIES,
)

DISPATCH_REQUIRED_EVENTS = {
    "task_started",
    "task_complete",
    "task_failed",
    "task_timeout",
    "task_blocked",
    "dispatch_sent",
    "dispatch_ack",
    "ack",
}

STATE_MUTATION_EVENTS = {"state_mutation"}

# ADR-035 §3.2.1/§6.1 (r3 BLOCKING-1): a typo/garbage guard on the resolved
# event-name value, never a membership/allow-list check — see §3.2.1 for why
# a closed `LEGAL_EVENT_TYPES` enum was rejected (50+ live event_type
# literals across the tree, growing).
_EVENT_NAME_FORMAT = re.compile(r"^[a-z][a-z0-9_]*$")

def _requires_dispatch_id(receipt: Dict[str, Any], event_name: str) -> bool:
    if event_name in DISPATCH_REQUIRED_EVENTS:
        return True
    if event_name.startswith("task_"):
        return True
    if receipt.get("task_id"):
        return True
    return False


def _warn_if_review_gate_missing_dispatch_id(event_name: str, receipt: Dict[str, Any]) -> None:
    import sys as _sys
    candidates = [m for m in (_sys.modules.get("append_receipt"), get_facade_module()) if m is not None]
    if any(getattr(m, "_warned_review_gate_no_dispatch_id", False) for m in candidates):
        return
    if event_name == "review_gate_request":
        if not str(receipt.get("dispatch_id", "")).strip():
            for m in candidates:
                m._warned_review_gate_no_dispatch_id = True
            facade._emit(
                "WARN",
                "review_gate_request_missing_dispatch_id",
                message="review_gate_request receipt has no dispatch_id — receipt-to-gate audit linkage severed",
            )


def _resolve_schema_version(receipt: Dict[str, Any]) -> int:
    """Absent/non-numeric `schema_version` means legacy v1 shape (ADR-035 §7)."""
    raw = receipt.get("schema_version")
    if raw is None:
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


# ADR-035 §3.3/§9 PR-5: the field set the v2 shape cutover trimmed. A
# `schema_version >= 2` receipt carrying any of these is the mixed "v1.5"
# shape HIGH-6/T27 forbid — the version stamp and the trimmed shape must
# land together, never one ahead of the other. `provenance.captured_at`/
# `captured_by` are the one trimmed pair that live under a subkey rather
# than top-level, so they are checked separately below.
LEGACY_TRIMMED_TOP_LEVEL_FIELDS = frozenset({
    "session",
    "validation",
    "recorded_at",
    "quality_advisory",
    "confidence",
    "tags",
    "root_cause",
    "dependencies",
    "metrics",
    "prevention_rules",
    "used_pattern_hashes",
    "legacy_format",
})

LEGACY_TRIMMED_PROVENANCE_SUBKEYS = frozenset({"captured_at", "captured_by"})


def _reject_legacy_fields_on_v2(receipt: Dict[str, Any], schema_version: int) -> None:
    """Fail-closed (reject, write nothing) rather than silently strip: a
    caller that sets a trimmed legacy field on a v2-stamped receipt has a
    bug that must surface loudly, not get silently corrected. `schema_version`
    absent/`1` is unaffected — v1 lines keep full tolerance for these fields
    (append-only, the past is never rewritten)."""
    if schema_version < 2:
        return

    offending = sorted(field for field in LEGACY_TRIMMED_TOP_LEVEL_FIELDS if field in receipt)

    provenance = receipt.get("provenance")
    if isinstance(provenance, dict):
        offending.extend(
            sorted(
                f"provenance.{subkey}"
                for subkey in LEGACY_TRIMMED_PROVENANCE_SUBKEYS
                if subkey in provenance
            )
        )

    if offending:
        raise AppendReceiptError(
            "legacy_field_on_v2_receipt",
            EXIT_VALIDATION_ERROR,
            f"schema_version={schema_version} receipt carries trimmed legacy "
            f"field(s) removed by ADR-035 §9 PR-5: {', '.join(offending)}",
        )


def _resolve_event_name(receipt: Dict[str, Any], schema_version: int) -> str:
    """ADR-035 §3.2.1 (r3 HIGH-2): for `schema_version >= 2`, `event_type`
    alone is consulted — the legacy `event` alias is NOT a fallback for a
    v2-shaped record. For `schema_version` absent/`1`, both keys are
    consulted exactly as v1 always has (`event_type` first, `event` as
    fallback) — this alias tolerance is unconditionally unchanged for v1."""
    if schema_version >= 2:
        return str(receipt.get("event_type") or "").strip()
    return str(receipt.get("event_type") or receipt.get("event") or "").strip()


def _validate_warning_entry(entry: Any, index: int) -> None:
    """ADR-035 §6.1's full reject list for one `warnings[]` entry.

    Stateless by design (§6.1): no rolling-window read here — the
    destination-assignment engine (warning_destination.py) is the one
    place that reads recurrence history and stamps `requires_tracking`;
    this validator only checks the structural/matrix invariants against
    what is already on the entry.
    """
    if not isinstance(entry, dict):
        raise AppendReceiptError(
            "invalid_warning_shape",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}] must be an object",
        )

    for required_key in ("code", "severity", "requires_tracking"):
        if required_key not in entry:
            raise AppendReceiptError(
                "missing_required_key",
                EXIT_VALIDATION_ERROR,
                f"warnings[{index}] missing required key: {required_key}",
            )

    severity = entry.get("severity")
    if severity not in LEGAL_SEVERITIES:
        raise AppendReceiptError(
            "invalid_warning_severity",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}].severity={severity!r} is not one of "
            f"{sorted(LEGAL_SEVERITIES)}",
        )

    if "destination" not in entry:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}] missing required key: destination",
        )

    destination = entry.get("destination")
    if destination not in LEGAL_DESTINATIONS:
        raise AppendReceiptError(
            "invalid_warning_destination",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}].destination={destination!r} is not one of "
            f"{sorted(LEGAL_DESTINATIONS)}",
        )

    if destination == "oi" and not entry.get("oi_id"):
        raise AppendReceiptError(
            "invalid_warning_oi_missing_id",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}].destination='oi' requires a non-null oi_id",
        )

    if destination == "oi_pending":
        if entry.get("oi_id") is not None:
            raise AppendReceiptError(
                "invalid_warning_oi_pending_shape",
                EXIT_VALIDATION_ERROR,
                f"warnings[{index}].destination='oi_pending' requires oi_id=null "
                f"(got {entry.get('oi_id')!r})",
            )
        if entry.get("reason") is None:
            raise AppendReceiptError(
                "invalid_warning_oi_pending_shape",
                EXIT_VALIDATION_ERROR,
                f"warnings[{index}].destination='oi_pending' requires a "
                "non-null reason",
            )

    if destination == "dropped":
        reason = entry.get("reason")
        if reason is None or reason not in DROP_REASON_ALLOWLIST:
            raise AppendReceiptError(
                "invalid_warning_dropped_reason",
                EXIT_VALIDATION_ERROR,
                f"warnings[{index}].destination='dropped' requires a reason "
                f"from {sorted(DROP_REASON_ALLOWLIST)}, got {reason!r}",
            )

    requires_tracking = entry.get("requires_tracking")

    if requires_tracking is True and destination in ("counted", "dropped"):
        raise AppendReceiptError(
            "invalid_warning_tracking_destination_mismatch",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}].requires_tracking=True is incompatible with "
            f"destination={destination!r}",
        )

    if severity == "blocker" and requires_tracking is not True:
        raise AppendReceiptError(
            "invalid_warning_severity_tracking_mismatch",
            EXIT_VALIDATION_ERROR,
            f"warnings[{index}].severity='blocker' requires "
            f"requires_tracking=True (got {requires_tracking!r})",
        )


def _validate_warnings(receipt: Dict[str, Any]) -> None:
    warnings_list = receipt.get("warnings")
    if not warnings_list:
        return
    for index, entry in enumerate(warnings_list):
        _validate_warning_entry(entry, index)


def _validate_receipt(receipt: Dict[str, Any]) -> str:
    timestamp = str(receipt.get("timestamp", "")).strip()
    if not timestamp:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: timestamp",
        )

    schema_version = _resolve_schema_version(receipt)
    _reject_legacy_fields_on_v2(receipt, schema_version)
    event_name = _resolve_event_name(receipt, schema_version)
    if not event_name:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: event_type"
            if schema_version >= 2
            else "Missing required key: event_type or event",
        )

    if not _EVENT_NAME_FORMAT.match(event_name):
        raise AppendReceiptError(
            "invalid_event_type_format",
            EXIT_VALIDATION_ERROR,
            f"event name {event_name!r} does not match ^[a-z][a-z0-9_]*$",
        )

    _validate_warnings(receipt)

    if _requires_dispatch_id(receipt, event_name):
        dispatch_id = str(receipt.get("dispatch_id", "")).strip()
        if not dispatch_id:
            raise AppendReceiptError(
                "missing_required_key",
                EXIT_VALIDATION_ERROR,
                "Missing required key: dispatch_id",
            )

    _warn_if_review_gate_missing_dispatch_id(event_name, receipt)

    return event_name


def _is_completion_event(receipt: Dict[str, Any]) -> bool:
    event_type = receipt.get("event_type") or receipt.get("event") or ""
    return event_type in (
        "task_complete",
        "task_completed",
        "completion",
        "complete",
        "subprocess_completion",
    )


def _is_subprocess_intermediate_completion(receipt: Dict[str, Any]) -> bool:
    """True for the intermediate subprocess-adapter completion receipt."""
    event_type = receipt.get("event_type") or receipt.get("event") or ""
    return event_type == "subprocess_completion"
