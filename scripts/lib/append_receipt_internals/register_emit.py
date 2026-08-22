"""Dispatch register event emission for codex_gate-relevant receipts."""

from __future__ import annotations

import sys

from .common import REPO_ROOT, _emit


def _emit_dispatch_register(receipt: dict) -> bool:
    """Emit dispatch_register event for lifecycle-relevant receipts.

    Maps receipt event_type → dispatch_register event (dispatch_completed,
    dispatch_failed, dispatch_started, gate_requested).  Called unconditionally
    for every appended (or duplicate-skipped, OI-1425) receipt (before the
    skip_enrichment gate).

    Returns True on success, False on any failure OR when this event_type
    carries no register mapping (best-effort, never raises). OI-1425: a
    `return False` from "nothing to map here" (expected, silent) is no longer
    indistinguishable from a `return False` caused by a real failure (import
    error, bad receipt shape, register write failure) — the latter is always
    logged loud via `_emit(WARN, ...)` first, fail-open on the caller's append
    path but never silent.
    """
    dispatch_id = str(receipt.get("dispatch_id", ""))

    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from dispatch_register import append_event
        from event_outcome_semantics import classify_event_outcome, COMPLETION_EVENT_TYPES
    except Exception as exc:
        _emit("WARN", "dispatch_register_emit_failed", stage="import",
              dispatch_id=dispatch_id, error=str(exc))
        return False

    try:
        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()
        gate = str(receipt.get("gate", "")).lower()
        terminal = str(receipt.get("terminal", ""))
        feature_id = str(receipt.get("feature_id", ""))
        pr_number = receipt.get("pr_number")
        if pr_number is None:
            pr_number = receipt.get("metadata", {}).get("pr_number") if isinstance(receipt.get("metadata"), dict) else None
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (ValueError, TypeError):
            pr_number = None

        # OI-1148: event_type + status -> governed outcome is decided in ONE
        # place (event_outcome_semantics.classify_event_outcome), not
        # re-derived here. This also fixes two drifted behaviours this module
        # used to own independently: "done" (the actual success literal the
        # tmux-interactive lane's completion receipt uses) was missing from
        # this module's own SUCCESS_STATUSES, silently dropping every such
        # receipt from the register instead of recording dispatch_completed;
        # and task_timeout was unconditionally mapped to dispatch_failed even
        # when status="no_confirmation" (a pending/blocked state per
        # rp_state.sh, not a terminal failure).
        OUTCOME_EVENT_TYPES = COMPLETION_EVENT_TYPES | {
            "task_failed", "task_timeout", "report_contract_invalid",
        }

        register_event = None
        if event_type in OUTCOME_EVENT_TYPES:
            outcome = classify_event_outcome(event_type, status)
            if outcome == "failure":
                register_event = "dispatch_failed"
            elif outcome == "success":
                register_event = "dispatch_completed"
            else:
                return False  # no outcome signal yet — expected, not an error
        elif event_type in ("task_started", "task_start", "dispatch_start"):
            register_event = "dispatch_started"
        elif event_type == "review_gate_request":
            if gate != "codex_gate":
                return False  # other gates intentionally not register-tracked
            register_event = "gate_requested"
        else:
            return False  # event_type has no register mapping — expected, not an error
    except Exception as exc:
        _emit("WARN", "dispatch_register_emit_failed", stage="classify",
              dispatch_id=dispatch_id, error=str(exc))
        return False

    try:
        return append_event(
            register_event,
            dispatch_id=dispatch_id,
            pr_number=pr_number,
            feature_id=feature_id,
            terminal=terminal,
            gate=gate,
        )
    except Exception as exc:
        _emit("WARN", "dispatch_register_emit_failed", stage="append",
              dispatch_id=dispatch_id, register_event=register_event, error=str(exc))
        return False
