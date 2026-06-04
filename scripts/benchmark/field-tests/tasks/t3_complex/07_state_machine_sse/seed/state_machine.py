from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar


class InvalidTransition(Exception):
    """Raised when an action is not allowed from the current state.

    The message always names the current state and the attempted action so
    callers can log the rejected request without re-deriving context.
    """


class ReviewableDocument:
    """A document moving through a strict 5-state review workflow.

    States::

        DRAFT ──submit──► PENDING_REVIEW
        PENDING_REVIEW ──approve──► APPROVED
        PENDING_REVIEW ──request_changes──► CHANGES_REQUESTED
        PENDING_REVIEW ──reject──► REJECTED
        CHANGES_REQUESTED ──submit──► PENDING_REVIEW
        APPROVED ──(terminal)
        REJECTED ──(terminal)

    The transition table below is the single source of truth: the set of
    actions allowed from a given state is *derived* from it, so there is no
    second map to drift out of sync. Terminal states simply have no entry as
    a source, so every action from them raises ``InvalidTransition``.

    Each successful ``transition`` records an audit entry in memory together
    with the moment it occurred. The entries are written to durable storage
    atomically by ``persistence.save_document`` — state and audit can never
    diverge because the state mutation and the audit append happen together,
    with no I/O between them, and are flushed in a single DB transaction.
    """

    INITIAL_STATE: ClassVar[str] = "DRAFT"

    STATES: ClassVar[frozenset[str]] = frozenset(
        {"DRAFT", "PENDING_REVIEW", "APPROVED", "CHANGES_REQUESTED", "REJECTED"}
    )

    # (from_state, action) -> to_state. The only place transitions are defined.
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
        self.state = self.INITIAL_STATE
        # Audit entries accrued since the last persistence flush.
        self._pending: list[dict] = []

    @classmethod
    def allowed_actions(cls, state: str) -> set[str]:
        """Return the actions permitted from ``state`` (empty for terminals)."""
        return {action for (src, action) in cls._TRANSITIONS if src == state}

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply ``action`` to this document or raise ``InvalidTransition``.

        On success the state is updated and an audit entry — stamped with the
        moment of transition — is appended atomically (no I/O in between).
        """
        to_state = self._TRANSITIONS.get((self.state, action))
        if to_state is None:
            allowed = sorted(self.allowed_actions(self.state))
            allowed_desc = ", ".join(allowed) if allowed else "none (terminal state)"
            raise InvalidTransition(
                f"Action {action!r} is not allowed from state {self.state!r}; "
                f"allowed actions: {allowed_desc}"
            )

        from_state = self.state
        occurred_at = datetime.now(timezone.utc).isoformat()

        self.state = to_state
        self._pending.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "reason": reason,
                "occurred_at": occurred_at,
            }
        )

    def drain_pending(self) -> list[dict]:
        """Return audit entries accrued since the last flush and clear them."""
        entries = self._pending
        self._pending = []
        return entries
