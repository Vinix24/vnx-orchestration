#!/usr/bin/env python3
"""VNX Runtime State Machine — dispatch state validation, transitions, and attempt tracking."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from coordination_db import (
    ACCEPTED_OR_BEYOND_STATES,
    DISPATCH_STATES,
    DISPATCH_TRANSITIONS,
    LEASE_STATES,
    LEASE_TRANSITIONS,
    TERMINAL_DISPATCH_STATES,
    DuplicateTransitionError,
    InvalidStateError,
    InvalidTransitionError,
    _append_event,
    _dump,
    _new_event_id,
    _now_utc,
)


def validate_dispatch_state(state: str) -> None:
    if state not in DISPATCH_STATES:
        raise InvalidStateError(f"Unknown dispatch state: {state!r}. Valid: {sorted(DISPATCH_STATES)}")


def validate_lease_state(state: str) -> None:
    if state not in LEASE_STATES:
        raise InvalidStateError(f"Unknown lease state: {state!r}. Valid: {sorted(LEASE_STATES)}")


def validate_dispatch_transition(from_state: str, to_state: str) -> None:
    validate_dispatch_state(from_state)
    validate_dispatch_state(to_state)
    allowed = DISPATCH_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidTransitionError(
            f"Dispatch transition {from_state!r} -> {to_state!r} is not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal state)'}"
        )


def is_terminal_dispatch_state(state: str) -> bool:
    """Return True if the dispatch state has no outgoing transitions."""
    return state in TERMINAL_DISPATCH_STATES


def is_accepted_or_beyond(state: str) -> bool:
    """Return True if the dispatch has already been accepted or progressed past acceptance."""
    return state in ACCEPTED_OR_BEYOND_STATES


def validate_lease_transition(from_state: str, to_state: str) -> None:
    validate_lease_state(from_state)
    validate_lease_state(to_state)
    allowed = LEASE_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidTransitionError(
            f"Lease transition {from_state!r} -> {to_state!r} is not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal state)'}"
        )


def register_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    terminal_id: Optional[str] = None,
    track: Optional[str] = None,
    priority: str = "P2",
    pr_ref: Optional[str] = None,
    gate: Optional[str] = None,
    bundle_path: Optional[str] = None,
    expires_after: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Register a new dispatch in the queued state (idempotent)."""
    existing = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    if existing:
        return dict(existing)

    now = _now_utc()
    conn.execute(
        """
        INSERT INTO dispatches
            (dispatch_id, state, terminal_id, track, priority, pr_ref, gate,
             bundle_path, expires_after, created_at, updated_at, metadata_json)
        VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (dispatch_id, terminal_id, track, priority, pr_ref, gate,
         bundle_path, expires_after, now, now, _dump(metadata)),
    )
    _append_event(
        conn, event_type="dispatch_queued", entity_type="dispatch",
        entity_id=dispatch_id, from_state=None, to_state="queued",
        actor=actor, reason="initial registration", metadata=metadata,
    )
    return dict(
        conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
    )


def transition_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    to_state: str,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Transition a dispatch to a new state, validating and emitting an event."""
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Dispatch not found: {dispatch_id!r}")

    from_state = row["state"]
    validate_dispatch_transition(from_state, to_state)

    now = _now_utc()
    conn.execute(
        "UPDATE dispatches SET state = ?, updated_at = ? WHERE dispatch_id = ?",
        (to_state, now, dispatch_id),
    )
    _append_event(
        conn, event_type=f"dispatch_{to_state}", entity_type="dispatch",
        entity_id=dispatch_id, from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata,
    )
    return dict(
        conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
    )


def _check_idempotent_noop(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: Optional[str],
    metadata: Optional[Dict[str, Any]],
    row: sqlite3.Row,
) -> Optional[Dict[str, Any]]:
    """Return the row dict if this is a no-op transition, else None."""
    if from_state == to_state:
        _append_event(
            conn, event_type="dispatch_noop", entity_type="dispatch",
            entity_id=dispatch_id, from_state=from_state, to_state=to_state,
            actor=actor, reason=reason or f"idempotent no-op: already in {to_state!r}",
            metadata=metadata,
        )
        return dict(row)
    if is_terminal_dispatch_state(from_state):
        raise DuplicateTransitionError(
            f"Dispatch {dispatch_id!r} is in terminal state {from_state!r}; "
            f"cannot transition to {to_state!r}",
            dispatch_id=dispatch_id, current_state=from_state, requested_state=to_state,
        )
    if to_state == "accepted" and is_accepted_or_beyond(from_state):
        _append_event(
            conn, event_type="dispatch_noop", entity_type="dispatch",
            entity_id=dispatch_id, from_state=from_state, to_state=to_state,
            actor=actor,
            reason=reason or f"idempotent no-op: already at {from_state!r} (past {to_state!r})",
            metadata=metadata,
        )
        return dict(row)
    return None


def transition_dispatch_idempotent(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    to_state: str,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Idempotent dispatch transition: no-op if already at or beyond target state."""
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Dispatch not found: {dispatch_id!r}")

    from_state = row["state"]
    noop = _check_idempotent_noop(
        conn, dispatch_id=dispatch_id, from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata, row=row,
    )
    if noop is not None:
        return noop
    return transition_dispatch(
        conn, dispatch_id=dispatch_id, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata,
    )


def increment_attempt_count(conn: sqlite3.Connection, dispatch_id: str) -> int:
    """Increment attempt_count for a dispatch. Returns new count."""
    conn.execute(
        "UPDATE dispatches SET attempt_count = attempt_count + 1, updated_at = ? WHERE dispatch_id = ?",
        (_now_utc(), dispatch_id),
    )
    row = conn.execute(
        "SELECT attempt_count FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    return row["attempt_count"] if row else 0


def create_attempt(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    terminal_id: str,
    attempt_number: int,
    metadata: Optional[Dict[str, Any]] = None,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Create a new dispatch attempt record."""
    attempt_id = _new_event_id()
    now = _now_utc()
    conn.execute(
        """
        INSERT INTO dispatch_attempts
            (attempt_id, dispatch_id, attempt_number, terminal_id, state, started_at, metadata_json)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (attempt_id, dispatch_id, attempt_number, terminal_id, now, _dump(metadata)),
    )
    _append_event(
        conn, event_type="attempt_created", entity_type="attempt",
        entity_id=attempt_id, from_state=None, to_state="pending", actor=actor,
        reason=f"attempt {attempt_number} for dispatch {dispatch_id}",
        metadata={"dispatch_id": dispatch_id, "terminal_id": terminal_id, "attempt_number": attempt_number},
    )
    return dict(
        conn.execute("SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    )


def update_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    state: str,
    failure_reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Update a dispatch attempt state and optionally record failure reason."""
    row = conn.execute(
        "SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Attempt not found: {attempt_id!r}")

    from_state = row["state"]
    now = _now_utc()
    conn.execute(
        "UPDATE dispatch_attempts SET state = ?, ended_at = ?, failure_reason = ? WHERE attempt_id = ?",
        (state, now, failure_reason, attempt_id),
    )
    event_type = "attempt_failed" if state == "failed" else f"attempt_{state}"
    _append_event(
        conn, event_type=event_type, entity_type="attempt",
        entity_id=attempt_id, from_state=from_state, to_state=state,
        actor=actor, reason=failure_reason, metadata=metadata,
    )
    return dict(
        conn.execute("SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    )
