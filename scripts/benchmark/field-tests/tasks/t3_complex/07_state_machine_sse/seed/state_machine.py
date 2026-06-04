"""ReviewableDocument: HITL review state-machine.

The transition graph::

    DRAFT             ──submit──►          PENDING_REVIEW
    PENDING_REVIEW    ──approve──►         APPROVED            (terminal)
    PENDING_REVIEW    ──request_changes──► CHANGES_REQUESTED
    PENDING_REVIEW    ──reject──►          REJECTED            (terminal)
    CHANGES_REQUESTED ──submit──►          PENDING_REVIEW
    APPROVED                               (terminal — no outgoing actions)
    REJECTED                               (terminal — no outgoing actions)

A single ``(from_state, action) -> to_state`` table is the only place
transitions are declared; both the allowed-action set per state and the
terminal-state check are derived from it, so the rules can't drift.

Every successful ``transition()`` buffers an audit entry in memory.
``persistence.save_document`` drains the buffer and writes the state row plus
all buffered audit rows inside one SQLite transaction, so the persisted
state can never disagree with the audit trail.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the current state."""


class ReviewableDocument:
    INITIAL_STATE: ClassVar[str] = "DRAFT"

    STATES: ClassVar[frozenset[str]] = frozenset(
        {"DRAFT", "PENDING_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED"}
    )

    _TRANSITIONS: ClassVar[dict[tuple[str, str], str]] = {
        ("DRAFT", "submit"): "PENDING_REVIEW",
        ("PENDING_REVIEW", "approve"): "APPROVED",
        ("PENDING_REVIEW", "request_changes"): "CHANGES_REQUESTED",
        ("PENDING_REVIEW", "reject"): "REJECTED",
        ("CHANGES_REQUESTED", "submit"): "PENDING_REVIEW",
    }

    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state: str = self.INITIAL_STATE
        self._pending: list[dict] = []

    @classmethod
    def allowed_actions(cls, state: str) -> set[str]:
        """Return the action keys permitted from ``state`` (empty for terminals)."""
        return {action for (src, action) in cls._TRANSITIONS if src == state}

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls.STATES and not cls.allowed_actions(state)

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply ``action`` or raise ``InvalidTransition``.

        ``from_state`` in the audit entry is always the state immediately
        before this transition — captured before the in-place state mutation.
        """
        next_state = self._TRANSITIONS.get((self.state, action))
        if next_state is None:
            allowed = sorted(self.allowed_actions(self.state))
            allowed_desc = ", ".join(allowed) if allowed else "none (terminal state)"
            raise InvalidTransition(
                f"Action {action!r} is not allowed from state "
                f"{self.state!r}; allowed actions: {allowed_desc}"
            )

        from_state = self.state
        occurred_at = datetime.now(timezone.utc).isoformat()

        self.state = next_state
        self._pending.append(
            {
                "from_state": from_state,
                "to_state": next_state,
                "actor": actor,
                "reason": reason,
                "occurred_at": occurred_at,
            }
        )

    def drain_pending(self) -> list[dict]:
        """Return buffered audit entries and clear the buffer."""
        entries = self._pending
        self._pending = []
        return entries
