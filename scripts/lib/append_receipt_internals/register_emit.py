"""Dispatch register event emission for codex_gate-relevant receipts."""

from __future__ import annotations

import sys

from .common import REPO_ROOT


def _emit_dispatch_register(receipt: dict) -> bool:
    """Emit dispatch_register event for codex_gate-relevant receipts.

    SCOPE: codex_gate only. gemini_review and claude_github_optional are
    deferred until proper findings parsers exist (separate PR).

    Returns True on success, False on any failure (best-effort, never raises).
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        from dispatch_register import append_event

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

        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", ""}
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked"}

        register_event = None
        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                register_event = "dispatch_failed"
            elif status in SUCCESS_STATUSES:
                register_event = "dispatch_completed"
            else:
                return False
        elif event_type == "task_failed":
            register_event = "dispatch_failed"
        elif event_type == "task_timeout":
            register_event = "dispatch_failed"
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
