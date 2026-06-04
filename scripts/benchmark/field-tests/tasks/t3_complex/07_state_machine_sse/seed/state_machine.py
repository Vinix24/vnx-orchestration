"""Review state-machine with strict transition validation and audit capture.

A :class:`ReviewableDocument` moves through five states. All allowed moves
live in a single data-driven transition table; anything not in the table
raises :class:`InvalidTransition`. Every successful transition records an
audit entry (from_state, to_state, actor, reason, occurred_at) which the
persistence layer writes atomically together with the new state.
"""
from __future__ import annotations

from datetime import datetime, timezone

DRAFT = "DRAFT"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REJECTED = "REJECTED"

STATES: frozenset[str] = frozenset(
    {DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED}
)
TERMINAL_STATES: frozenset[str] = frozenset({APPROVED, REJECTED})

# Single source of truth for allowed moves: (from_state, action) -> to_state.
TRANSITIONS: dict[tuple[str, str], str] = {
    (DRAFT, "submit"): PENDING_REVIEW,
    (PENDING_REVIEW, "approve"): APPROVED,
    (PENDING_REVIEW, "request_changes"): CHANGES_REQUESTED,
    (PENDING_REVIEW, "reject"): REJECTED,
    (CHANGES_REQUESTED, "submit"): PENDING_REVIEW,
}


class InvalidTransition(Exception):
    """Raised when action is not allowed from current state."""


class ReviewableDocument:
    """A document with a validated review lifecycle and pending audit log.

    State changes happen only through :meth:`transition`; there is no other
    mutator, so every state change has a matching audit entry. The pending
    audit entries are drained by ``persistence.save_document`` after they
    have been committed.
    """

    def __init__(self, doc_id: int, project_id: str = "default"):
        self.doc_id = doc_id
        self.project_id = project_id
        self._state = DRAFT
        self._pending_audit: list[dict] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def pending_audit(self) -> tuple[dict, ...]:
        """Audit entries recorded since the last successful save."""
        return tuple(self._pending_audit)

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply transition or raise InvalidTransition."""
        to_state = TRANSITIONS.get((self._state, action))
        if to_state is None:
            if self._state in TERMINAL_STATES:
                raise InvalidTransition(
                    f"document {self.doc_id} is in terminal state "
                    f"{self._state}; action {action!r} is not allowed"
                )
            raise InvalidTransition(
                f"action {action!r} is not allowed from state {self._state}"
            )
        from_state = self._state
        self._state = to_state
        self._pending_audit.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "reason": reason,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def mark_persisted(self, count: int) -> None:
        """Drop the first ``count`` pending audit entries after a commit."""
        del self._pending_audit[:count]

    @classmethod
    def restore(cls, doc_id: int, project_id: str, state: str) -> "ReviewableDocument":
        """Rehydrate a persisted document. Not a transition: no audit entry."""
        if state not in STATES:
            raise ValueError(f"unknown persisted state {state!r}")
        doc = cls(doc_id, project_id)
        doc._state = state
        return doc
