"""Reviewable-document state machine.

Pure in-memory state machine. Persistence lives in `persistence.py`.

Design:
- Transitions are data-driven via a single TRANSITIONS table — no nested
  if/elif ladders.
- Terminal states (APPROVED, REJECTED) have no outgoing keys in the table,
  so any action from them raises InvalidTransition.
- `transition()` updates state AND queues an audit record. The audit row
  and the state change are flushed ATOMICALLY by `persistence.save_document`
  inside a single SQLite transaction. This avoids the audit-write-then-
  state-change race window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

# (from_state, action) -> to_state. Single source of truth.
TRANSITIONS: dict[tuple[str, str], str] = {
    (DRAFT, "submit"): PENDING_REVIEW,
    (PENDING_REVIEW, "approve"): APPROVED,
    (PENDING_REVIEW, "request_changes"): CHANGES_REQUESTED,
    (PENDING_REVIEW, "reject"): REJECTED,
    (CHANGES_REQUESTED, "submit"): PENDING_REVIEW,
}


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the current state."""


@dataclass
class TransitionRecord:
    from_state: str
    to_state: str
    actor: str
    reason: str
    occurred_at: str  # ISO-8601 UTC


@dataclass
class ReviewableDocument:
    doc_id: int
    project_id: str = "default"
    state: str = DRAFT
    pending_transitions: list[TransitionRecord] = field(default_factory=list)

    def __init__(self, doc_id: int, project_id: str = "default") -> None:
        self.doc_id = doc_id
        self.project_id = project_id
        self.state = DRAFT
        self.pending_transitions = []

    def allowed_actions(self) -> list[str]:
        return sorted(
            action for (from_state, action) in TRANSITIONS if from_state == self.state
        )

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply `action`. Raises InvalidTransition if not allowed.

        Side effects:
            - Updates self.state to the new state.
            - Appends a TransitionRecord to self.pending_transitions so the
              caller (persistence layer) can flush state + audit atomically.
        """
        key = (self.state, action)
        if key not in TRANSITIONS:
            allowed = self.allowed_actions()
            terminal_note = " (terminal state)" if self.state in TERMINAL_STATES else ""
            raise InvalidTransition(
                f"action {action!r} not allowed from state {self.state!r}"
                f"{terminal_note}; allowed actions: {allowed}"
            )
        from_state = self.state
        to_state = TRANSITIONS[key]
        self.pending_transitions.append(
            TransitionRecord(
                from_state=from_state,
                to_state=to_state,
                actor=actor,
                reason=reason,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.state = to_state
