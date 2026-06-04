"""Reviewable-document state machine.

A document moves through 5 states under 4 actions. Allowed (from, action) pairs
are listed in `TRANSITIONS`; any other combination raises `InvalidTransition`.
Terminal states have no outgoing transitions.

The state machine is pure: it owns no DB connection. Each call to `transition`
appends a row to `pending_transitions`. Persistence is the caller's job —
`persistence.save_document` writes the new state and any pending audit rows
atomically in a single transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Final


DRAFT: Final[str] = "DRAFT"
PENDING_REVIEW: Final[str] = "PENDING_REVIEW"
APPROVED: Final[str] = "APPROVED"
CHANGES_REQUESTED: Final[str] = "CHANGES_REQUESTED"
REJECTED: Final[str] = "REJECTED"

STATES: Final[frozenset[str]] = frozenset({
    DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED,
})
TERMINAL_STATES: Final[frozenset[str]] = frozenset({APPROVED, REJECTED})

SUBMIT: Final[str] = "submit"
APPROVE: Final[str] = "approve"
REQUEST_CHANGES: Final[str] = "request_changes"
REJECT: Final[str] = "reject"

TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    (DRAFT, SUBMIT): PENDING_REVIEW,
    (PENDING_REVIEW, APPROVE): APPROVED,
    (PENDING_REVIEW, REQUEST_CHANGES): CHANGES_REQUESTED,
    (PENDING_REVIEW, REJECT): REJECTED,
    (CHANGES_REQUESTED, SUBMIT): PENDING_REVIEW,
}


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the document's current state."""


class ReviewableDocument:
    """A document with a strict, auditable state lifecycle.

    `state` is mutated only through `transition`. Every successful transition
    appends an audit entry to `pending_transitions` carrying the from_state as
    it was *immediately before* the state change — never the post-change state.
    """

    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state: str = DRAFT
        self.pending_transitions: list[dict] = []

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        from_state = self.state
        if from_state in TERMINAL_STATES:
            raise InvalidTransition(
                f"document {self.doc_id} is in terminal state '{from_state}'; "
                f"action '{action}' is not allowed"
            )
        key = (from_state, action)
        to_state = TRANSITIONS.get(key)
        if to_state is None:
            raise InvalidTransition(
                f"action '{action}' is not allowed from state '{from_state}' "
                f"(document {self.doc_id})"
            )
        occurred_at = datetime.now(timezone.utc).isoformat()
        self.state = to_state
        self.pending_transitions.append({
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "reason": reason,
            "occurred_at": occurred_at,
        })
