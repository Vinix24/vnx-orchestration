"""append_receipt_payload pipeline + post-append hooks."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .common import (
    AppendReceiptError,
    AppendResult,
    EXIT_INVALID_INPUT,
    REPO_ROOT,
    SCRIPTS_DIR,
    _emit,
    facade,
)
from .idempotency import (
    _compute_idempotency_key,
    _cache_file_for,
    _resolve_receipts_file,
    _write_receipt_under_lock,
)
from .validation import _validate_receipt


def _maybe_reroute_to_gate_stream(receipt: Dict[str, Any], receipts_file: Optional[str]) -> Optional[str]:
    """Route ghost gate receipts (dispatch_id="unknown" + gate event) to gate_events.ndjson."""
    if receipts_file is not None or not facade.should_route_to_gate_stream(receipt):
        return receipts_file
    try:
        paths = facade.ensure_env()
        state_dir = Path(paths["VNX_STATE_DIR"])
        rerouted = str(facade.gate_events_file(state_dir))
        _emit("INFO", "ghost_receipt_rerouted",
              gate=str(receipt.get("gate") or ""),
              pr_id=str(receipt.get("pr_id") or ""),
              destination=rerouted)
        return rerouted
    except Exception as exc:
        _emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))
        return receipts_file


def _run_post_append_hooks(receipt: Dict[str, Any]) -> None:
    """Best-effort hooks fired after a receipt is successfully appended.

    Each hook is isolated: a failure in one does not prevent the others from
    running, and no exception is propagated to the caller. The NDJSON record
    is already durable at this point.
    """
    try:
        facade._register_quality_open_items(receipt)
    except Exception as exc:
        _emit("WARN", "oi_registration_post_hook_failed",
              dispatch_id=str(receipt.get("dispatch_id") or ""),
              error=str(exc))
    try:
        facade._update_confidence_from_receipt(receipt)
    except Exception as exc:
        _emit("WARN", "confidence_post_hook_failed", error=str(exc))
    try:
        facade._emit_dispatch_register(receipt)
    except Exception as exc:
        _emit("WARN", "dispatch_register_post_hook_failed", error=str(exc))
    try:
        facade._maybe_trigger_state_rebuild(receipt)
    except Exception:
        pass
    try:
        facade._trigger_receipt_classifier(receipt)
    except Exception:
        pass


def _write_central_receipt_mirror(receipt: Dict[str, Any], primary_path: Path) -> None:
    """Phase 6 P3: best-effort mirror of a receipt to the central per-project path.

    Writes to ~/.vnx-data/<project_id>/state/<filename> in addition to the
    existing per-project path.  Per-project remains source-of-truth; this
    never raises and never blocks the primary write.
    """
    project_id: Optional[str] = receipt.get("project_id") or os.environ.get("VNX_PROJECT_ID") or None
    if not project_id:
        return
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from vnx_paths import resolve_central_data_dir
        central_state = resolve_central_data_dir(project_id) / "state"
        central_state.mkdir(parents=True, exist_ok=True)
        central_path = central_state / primary_path.name
        if central_path.resolve() == primary_path.resolve():
            return
        with central_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(receipt, separators=(",", ":"), sort_keys=False) + "\n")
    except Exception:
        pass


def _stamp_identity(receipt: Dict[str, Any]) -> None:
    """Backfill the four-tuple identity fields on a receipt in place.

    Phase 6 P2: every NDJSON line should be attributable to
    {operator, project, orchestrator, agent}. Resolution is best-effort —
    when ``vnx_identity.try_resolve_identity()`` returns None (no env,
    no ``.vnx-project-id``, no registry hit), the receipt is written
    without identity fields rather than blocking the durability path.
    Caller-supplied values are never overwritten. Fields with no value
    are NOT serialized (we do not stamp ``"operator_id": null``).
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from vnx_identity import try_resolve_identity
    except Exception:
        return

    identity = try_resolve_identity()
    if identity is None:
        return

    if not receipt.get("operator_id"):
        receipt["operator_id"] = identity.operator_id
    if not receipt.get("project_id"):
        receipt["project_id"] = identity.project_id
    if not receipt.get("orchestrator_id") and identity.orchestrator_id:
        receipt["orchestrator_id"] = identity.orchestrator_id
    if not receipt.get("agent_id") and identity.agent_id:
        receipt["agent_id"] = identity.agent_id


def append_receipt_payload(
    receipt: Dict[str, Any],
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
    skip_enrichment: bool = False,
) -> AppendResult:
    if not isinstance(receipt, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    _stamp_identity(receipt)

    if not skip_enrichment:
        receipt = facade._enrich_completion_receipt(receipt)

    receipt.setdefault("open_items_created", facade._count_quality_violations(receipt))

    receipts_file = _maybe_reroute_to_gate_stream(receipt, receipts_file)

    event_name = _validate_receipt(receipt)
    idempotency_key = _compute_idempotency_key(receipt, event_name)

    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_file_for(receipt_path)

    result = _write_receipt_under_lock(
        receipt,
        receipt_path,
        cache_path,
        idempotency_key,
        cache_window_seconds,
    )

    if result.status == "appended":
        # Phase 6 P3: best-effort mirror to central per-project path. Never blocks.
        _write_central_receipt_mirror(receipt, receipt_path)
        if not skip_enrichment:
            _run_post_append_hooks(receipt)

    return result


def _update_confidence_from_receipt(receipt: Dict[str, Any]) -> None:
    """Wire dispatch outcome into pattern confidence scores (best-effort)."""
    try:
        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", ""}
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked"}

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()

        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                outcome = "failure"
            elif status in SUCCESS_STATUSES:
                outcome = "success"
            else:
                return
        elif event_type == "task_failed":
            outcome = "failure"
        else:
            return

        dispatch_id = str(receipt.get("dispatch_id") or "")
        terminal = str(receipt.get("terminal") or "")
        if not dispatch_id:
            return

        state_dir = facade.resolve_state_dir(__file__)

        db_path = state_dir / "quality_intelligence.db"
        if not db_path.exists():
            return

        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from intelligence_persist import update_confidence_from_outcome
        update_confidence_from_outcome(db_path, dispatch_id, terminal, outcome)
    except Exception as exc:
        _emit("WARN", "confidence_update_failed", error=str(exc))


def _trigger_receipt_classifier(receipt: Dict[str, Any]) -> None:
    """Best-effort fire of the adaptive receipt classifier (ARC-3).

    Disabled by default; opt-in via VNX_RECEIPT_CLASSIFIER_ENABLED=1. Never
    raises — the receipt writer must remain on its happy path even if the
    classifier import or subprocess spawn fails.
    """
    if os.environ.get("VNX_RECEIPT_CLASSIFIER_ENABLED", "0") != "1":
        return
    try:
        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        from receipt_classifier import trigger_receipt_classifier_async
        action = trigger_receipt_classifier_async(receipt)
        if action:
            _emit("INFO", "receipt_classifier_action", action=action)
    except Exception as exc:
        _emit("WARN", "receipt_classifier_trigger_failed", error=str(exc))


def _maybe_trigger_state_rebuild(receipt: Dict[str, Any]) -> None:
    """Trigger state rebuild via shared throttled helper. Best-effort."""
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()

    TRIGGER_EVENTS = {
        "task_complete", "task_completed", "completion", "complete",
        "task_failed", "task_timeout",
        "dispatch_promoted", "dispatch_started",
    }
    if event_type not in TRIGGER_EVENTS:
        return

    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from state_rebuild_trigger import maybe_trigger_state_rebuild
        maybe_trigger_state_rebuild(event_type=event_type)
    except Exception:
        pass
