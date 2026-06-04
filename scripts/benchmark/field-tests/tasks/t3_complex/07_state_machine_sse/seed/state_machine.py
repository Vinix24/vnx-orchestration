from __future__ import annotations

from datetime import datetime, timezone

TRANSITIONS: dict[tuple[str, str], str] = {
    ("DRAFT", "submit"): "PENDING_REVIEW",
    ("PENDING_REVIEW", "approve"): "APPROVED",
    ("PENDING_REVIEW", "request_changes"): "CHANGES_REQUESTED",
    ("PENDING_REVIEW", "reject"): "REJECTED",
    ("CHANGES_REQUESTED", "submit"): "PENDING_REVIEW",
}

TERMINAL_STATES: frozenset[str] = frozenset({"APPROVED", "REJECTED"})


class InvalidTransition(Exception):
    """Raised when action is not allowed from current state."""


class ReviewableDocument:
    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state = "DRAFT"
        self._pending_audit: list[dict] = []

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply transition or raise InvalidTransition."""
        if self.state in TERMINAL_STATES:
            raise InvalidTransition(
                f"State '{self.state}' is terminal; action '{action}' is not allowed."
            )

        key = (self.state, action)
        if key not in TRANSITIONS:
            raise InvalidTransition(
                f"Action '{action}' is not allowed from state '{self.state}'."
            )

        from_state = self.state
        to_state = TRANSITIONS[key]

        self._pending_audit.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "reason": reason,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.state = to_state
