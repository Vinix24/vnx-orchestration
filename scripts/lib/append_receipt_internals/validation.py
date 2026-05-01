"""Receipt validation, completion-event detection, and ghost-receipt routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .common import (
    AppendReceiptError,
    EXIT_VALIDATION_ERROR,
    facade,
    get_facade_module,
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


def _validate_receipt(receipt: Dict[str, Any]) -> str:
    timestamp = str(receipt.get("timestamp", "")).strip()
    if not timestamp:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: timestamp",
        )

    event_name = str(receipt.get("event_type") or receipt.get("event") or "").strip()
    if not event_name:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: event_type or event",
        )

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


def _maybe_reroute_ghost_receipt(receipt: Dict[str, Any], receipts_file: Optional[str]) -> Optional[str]:
    """Route ghost gate receipts (dispatch_id unset + gate event) to gate_events.ndjson."""
    if receipts_file is not None or not facade.should_route_to_gate_stream(receipt):
        return receipts_file
    try:
        paths = facade.ensure_env()
        state_dir = Path(paths["VNX_STATE_DIR"])
        rerouted = str(facade.gate_events_file(state_dir))
        facade._emit("INFO", "ghost_receipt_rerouted",
              gate=str(receipt.get("gate") or ""),
              pr_id=str(receipt.get("pr_id") or ""),
              destination=rerouted)
        return rerouted
    except Exception as exc:
        facade._emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))
        return receipts_file
