"""Reviewable-document state-machine.

Five states, strict allowed transitions, terminal APPROVED/REJECTED.
Transitions append to an in-memory audit buffer that persistence.save_document()
flushes to the state_transitions table.
"""
from __future__ import annotations

from datetime import datetime, timezone

DRAFT = "DRAFT"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REJECTED = "REJECTED"

ALL_STATES = frozenset({DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED})
TERMINAL_STATES = frozenset({APPROVED, REJECTED})

_TRANSITIONS: dict[str, dict[str, str]] = {
    DRAFT: {"submit": PENDING_REVIEW},
    PENDING_REVIEW: {
        "approve": APPROVED,
        "request_changes": CHANGES_REQUESTED,
        "reject": REJECTED,
    },
    CHANGES_REQUESTED: {"submit": PENDING_REVIEW},
    APPROVED: {},
    REJECTED: {},
}


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the current state."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewableDocument:
    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state = DRAFT
        now = _now_iso()
        self.created_at = now
        self.updated_at = now
        self.pending_transitions: list[dict] = []

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply transition or raise InvalidTransition.

        Audit row captures the pre-transition state as from_state; no shortcut
        bypasses the audit buffer.
        """
        allowed = _TRANSITIONS.get(self.state, {})
        if action not in allowed:
            raise InvalidTransition(
                f"action {action!r} not allowed from state {self.state!r}"
            )
        from_state = self.state
        to_state = allowed[action]
        occurred_at = _now_iso()
        self.pending_transitions.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "reason": reason,
                "occurred_at": occurred_at,
            }
        )
        self.state = to_state
        self.updated_at = occurred_at
