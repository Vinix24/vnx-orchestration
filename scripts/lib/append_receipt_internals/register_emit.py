"""Dispatch register event emission for codex_gate-relevant receipts."""

from __future__ import annotations

import sys

from .common import REPO_ROOT


def _emit_dispatch_register(receipt: dict) -> bool:
    """Emit dispatch_register event for lifecycle-relevant receipts.

    Maps receipt event_type → dispatch_register event (dispatch_completed,
    dispatch_failed, dispatch_started, gate_requested).  Called unconditionally
    for every appended receipt (before the skip_enrichment gate).

    Returns True on success, False on any failure (best-effort, never raises).
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from dispatch_register import append_event
        from event_outcome_semantics import classify_event_outcome, COMPLETION_EVENT_TYPES

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()
        gate = str(receipt.get("gate", "")).lower()
        dispatch_id = str(receipt.get("dispatch_id", ""))
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
                return False
        elif event_type in ("task_started", "task_start", "dispatch_start"):
            register_event = "dispatch_started"
        elif event_type == "review_gate_request":
            if gate != "codex_gate":
                return False
            register_event = "gate_requested"
        else:
            return False

        return append_event(
            register_event,
            dispatch_id=dispatch_id,
            pr_number=pr_number,
            feature_id=feature_id,
            terminal=terminal,
            gate=gate,
        )
    except Exception:
        return False
