"""Reviewable-document state-machine.

A document moves through 5 states under a fixed transition table. Every call to
:meth:`ReviewableDocument.transition` either applies the transition (mutating
``state`` and appending an audit entry to :attr:`_pending_audit`) or raises
:class:`InvalidTransition`. Persistence is the responsibility of
``persistence.save_document``, which drains :attr:`_pending_audit` and writes
the new ``state`` atomically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


DRAFT = "DRAFT"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REJECTED = "REJECTED"

STATES = frozenset({DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED})
TERMINAL_STATES = frozenset({APPROVED, REJECTED})

TRANSITIONS: dict[tuple[str, str], str] = {
    (DRAFT, "submit"): PENDING_REVIEW,
    (PENDING_REVIEW, "approve"): APPROVED,
    (PENDING_REVIEW, "request_changes"): CHANGES_REQUESTED,
    (PENDING_REVIEW, "reject"): REJECTED,
    (CHANGES_REQUESTED, "submit"): PENDING_REVIEW,
}


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the current state."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _PendingAudit:
    from_state: str
    to_state: str
    actor: str
    reason: str
    occurred_at: str


class ReviewableDocument:
    """In-memory state-machine for a reviewable document.

    The document carries its own pending audit log. ``transition`` mutates
    ``state`` and appends a pending entry in a single step; there is no public
    setter for ``state`` that bypasses the audit log.
    """

    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self._state = DRAFT
        self._pending_audit: list[_PendingAudit] = []
        now = _utcnow_iso()
        self.created_at = now
        self.updated_at = now

    @property
    def state(self) -> str:
        return self._state

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply ``action`` from the current state or raise InvalidTransition."""
        current = self._state
        if current in TERMINAL_STATES:
            raise InvalidTransition(
                f"document {self.doc_id} is in terminal state '{current}'; "
                f"action '{action}' is not allowed"
            )
        key = (current, action)
        next_state = TRANSITIONS.get(key)
        if next_state is None:
            raise InvalidTransition(
                f"action '{action}' is not allowed from state '{current}' "
                f"(document {self.doc_id})"
            )
        occurred_at = _utcnow_iso()
        self._state = next_state
        self.updated_at = occurred_at
        self._pending_audit.append(
            _PendingAudit(
                from_state=current,
                to_state=next_state,
                actor=actor,
                reason=reason,
                occurred_at=occurred_at,
            )
        )

    def drain_pending_audit(self) -> list[_PendingAudit]:
        """Return all unsaved audit entries and clear the buffer."""
        drained = self._pending_audit
        self._pending_audit = []
        return drained

    def has_pending_audit(self) -> bool:
        return bool(self._pending_audit)
