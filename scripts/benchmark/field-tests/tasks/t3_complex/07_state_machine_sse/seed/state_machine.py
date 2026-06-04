from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

DRAFT = "DRAFT"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REJECTED = "REJECTED"

TERMINAL_STATES: frozenset[str] = frozenset({APPROVED, REJECTED})

# Data-driven transition table: (from_state, action) -> to_state
TRANSITIONS: dict[tuple[str, str], str] = {
    (DRAFT, "submit"): PENDING_REVIEW,
    (PENDING_REVIEW, "approve"): APPROVED,
    (PENDING_REVIEW, "request_changes"): CHANGES_REQUESTED,
    (PENDING_REVIEW, "reject"): REJECTED,
    (CHANGES_REQUESTED, "submit"): PENDING_REVIEW,
}


class InvalidTransition(Exception):
    """Raised when action is not allowed from current state."""


@dataclass
class AuditEntry:
    from_state: str
    to_state: str
    actor: str
    timestamp: datetime
    reason: str


class ReviewableDocument:
    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state: str = DRAFT
        self._pending_audit: list[AuditEntry] = []

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply transition or raise InvalidTransition."""
        if self.state in TERMINAL_STATES:
            raise InvalidTransition(
                f"State '{self.state}' is terminal — action '{action}' is not allowed."
            )
        key = (self.state, action)
        if key not in TRANSITIONS:
            raise InvalidTransition(
                f"Action '{action}' is not allowed from state '{self.state}'."
            )
        from_state = self.state
        to_state = TRANSITIONS[key]
        # Capture audit entry BEFORE mutating state so from_state is always pre-transition
        self._pending_audit.append(
            AuditEntry(
                from_state=from_state,
                to_state=to_state,
                actor=actor,
                timestamp=datetime.now(timezone.utc),
                reason=reason,
            )
        )
        self.state = to_state
