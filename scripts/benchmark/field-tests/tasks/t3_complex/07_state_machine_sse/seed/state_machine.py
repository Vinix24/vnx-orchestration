from __future__ import annotations

from typing import ClassVar


class InvalidTransition(Exception):
    """Raised when action is not allowed from current state."""


class ReviewableDocument:
    """Document with a 5-state review workflow and strict transition rules.

    States: DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED

    Transitions:
        DRAFT ──submit──► PENDING_REVIEW
        PENDING_REVIEW ──approve──► APPROVED
        PENDING_REVIEW ──request_changes──► CHANGES_REQUESTED
        PENDING_REVIEW ──reject──► REJECTED
        CHANGES_REQUESTED ──submit──► PENDING_REVIEW
        APPROVED ──(terminal)──► (no outgoing transitions)
        REJECTED ──(terminal)──► (no outgoing transitions)
    """

    _ALLOWED: ClassVar[dict[str, set[str]]] = {
        "DRAFT": {"submit"},
        "PENDING_REVIEW": {"approve", "request_changes", "reject"},
        "CHANGES_REQUESTED": {"submit"},
        "APPROVED": set(),
        "REJECTED": set(),
    }

    _TRANSITION_MAP: ClassVar[dict[tuple[str, str], str]] = {
        ("DRAFT", "submit"): "PENDING_REVIEW",
        ("PENDING_REVIEW", "approve"): "APPROVED",
        ("PENDING_REVIEW", "request_changes"): "CHANGES_REQUESTED",
        ("PENDING_REVIEW", "reject"): "REJECTED",
        ("CHANGES_REQUESTED", "submit"): "PENDING_REVIEW",
    }

    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state = "DRAFT"
        self._audit: list[dict] = []

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        allowed = self._ALLOWED.get(self.state, set())
        if action not in allowed:
            raise InvalidTransition(
                f"Action '{action}' is not allowed from state '{self.state}'"
            )

        from_state = self.state
        to_state = self._TRANSITION_MAP[(self.state, action)]

        self.state = to_state
        self._audit.append({
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "reason": reason,
        })

    def flush_audit(self) -> list[dict]:
        """Return and clear accumulated audit entries."""
        entries = self._audit[:]
        self._audit.clear()
        return entries
