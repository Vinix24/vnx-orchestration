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
    _has_project_id_col,
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


#: Marker for "the caller did not pass ``project_id``": the transition keeps its
#: pre-ADR-049 dispatch_id-only behaviour. ``None`` is NOT this marker — a caller
#: that passes ``None`` names no project and is refused (see
#: :func:`_resolve_project_scope`).
_PROJECT_ID_UNSET: Any = object()


def _resolve_project_scope(project_id: Any) -> Optional[str]:
    """Turn the optional ``project_id`` keyword into a project scope.

    Returns the project id when the caller named one, ``None`` when the keyword
    was not given at all (legacy dispatch_id-only behaviour). A given but
    unusable value — ``None``, ``""``, blank, a non-string — raises: reading it
    as "no project" would silently widen the read and the write back to every
    project that shares the dispatch id, which is the bug this scope closes
    (fail closed on evidence).
    """
    if project_id is _PROJECT_ID_UNSET:
        return None
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError(
            f"project_id must be a non-empty string when given; got {project_id!r}"
        )
    return project_id


def _dispatch_row(
    conn: sqlite3.Connection, dispatch_id: str, project_id: Optional[str],
) -> Optional[sqlite3.Row]:
    """Read one dispatch row, scoped by project when a project scope is given."""
    if project_id is None:
        return conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (dispatch_id, project_id),
    ).fetchone()


def _dispatch_not_found(dispatch_id: str, project_id: Optional[str]) -> KeyError:
    """The not-found error, naming the project scope when there is one."""
    if project_id is None:
        return KeyError(f"Dispatch not found: {dispatch_id!r}")
    return KeyError(f"Dispatch not found: {dispatch_id!r} (project_id={project_id!r})")


def register_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    project_id: str,
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
    """Register a new dispatch in the queued state (idempotent).

    ADR-007: project_id is MANDATORY — no silent default. project_id is stamped
    on the INSERT row for column compliance.

    dispatch_id is GLOBALLY UNIQUE (timestamped slugs by construction). This
    registration read keys on dispatch_id alone ON PURPOSE: it is the guard that
    refuses cross-tenant reuse, so scoping it to the registering project would
    let a second row with the same dispatch_id slip in under another project.

    If a row with the same dispatch_id exists under a DIFFERENT project_id,
    ValueError is raised — cross-tenant dispatch_id reuse is a bug, not a
    silent merge.

    The transition path is project-scoped since ADR-049 K-3a (an optional
    ``project_id`` on ``transition_dispatch`` / ``transition_dispatch_idempotent``);
    the remaining dispatch_id-keyed operations are deferred to a post-launch
    hardening item.
    """
    has_pid = _has_project_id_col(conn, "dispatches")

    # Global on purpose — this is the cross-tenant reuse guard (see docstring).
    existing = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    if existing:
        if has_pid:
            existing_pid = existing["project_id"]
            if existing_pid and existing_pid != project_id:
                raise ValueError(
                    f"Cross-tenant dispatch_id reuse: {dispatch_id!r} is already registered "
                    f"under project {existing_pid!r}; refusing registration under {project_id!r}"
                )
        return dict(existing)

    now = _now_utc()
    if has_pid:
        conn.execute(
            """
            INSERT INTO dispatches
                (dispatch_id, project_id, state, terminal_id, track, priority, pr_ref, gate,
                 bundle_path, expires_after, created_at, updated_at, metadata_json)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (dispatch_id, project_id, terminal_id, track, priority, pr_ref, gate,
             bundle_path, expires_after, now, now, _dump(metadata)),
        )
    else:
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
        project_id=project_id,
    )
    return dict(
        conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
    )


def _transition_dispatch_scoped(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    to_state: str,
    actor: str,
    reason: Optional[str],
    metadata: Optional[Dict[str, Any]],
    project_id: Optional[str],
) -> Dict[str, Any]:
    """The transition body with the project scope already resolved.

    ``project_id`` is a validated project id, or ``None`` for the legacy
    dispatch_id-only behaviour.
    """
    row = _dispatch_row(conn, dispatch_id, project_id)
    if row is None:
        raise _dispatch_not_found(dispatch_id, project_id)

    from_state = row["state"]
    validate_dispatch_transition(from_state, to_state)

    now = _now_utc()
    sql = "UPDATE dispatches SET state = ?, updated_at = ? WHERE dispatch_id = ?"
    args: tuple = (to_state, now, dispatch_id)
    if project_id is not None:
        sql += " AND project_id = ?"
        args += (project_id,)
    cursor = conn.execute(sql, args)
    if project_id is not None and cursor.rowcount == 0:
        # The scoped read saw the row; a concurrent writer removed it before this
        # UPDATE ran. Never report a success the store did not record.
        raise _dispatch_not_found(dispatch_id, project_id)
    _append_event(
        conn, event_type=f"dispatch_{to_state}", entity_type="dispatch",
        entity_id=dispatch_id, from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata, project_id=project_id,
    )
    return dict(_dispatch_row(conn, dispatch_id, project_id))


def transition_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    to_state: str,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    project_id: Any = _PROJECT_ID_UNSET,
) -> Dict[str, Any]:
    """Transition a dispatch to a new state, validating and emitting an event.

    ``project_id`` scopes the read, the UPDATE, the returned row and the event
    (ADR-007/ADR-049): with a project given, a row of another project that
    carries the same ``dispatch_id`` is never read or written, and an UPDATE
    that touches no row is the not-found error. Not passing it keeps the
    pre-ADR-049 dispatch_id-only behaviour for the callers not migrated yet. A
    ``project_id`` that is ``None``, empty or not a string is refused: naming no
    project must never silently mean "every project".
    """
    return _transition_dispatch_scoped(
        conn,
        dispatch_id=dispatch_id,
        to_state=to_state,
        actor=actor,
        reason=reason,
        metadata=metadata,
        project_id=_resolve_project_scope(project_id),
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
    project_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the row dict if this is a no-op transition, else None."""
    if from_state == to_state:
        _append_event(
            conn, event_type="dispatch_noop", entity_type="dispatch",
            entity_id=dispatch_id, from_state=from_state, to_state=to_state,
            actor=actor, reason=reason or f"idempotent no-op: already in {to_state!r}",
            metadata=metadata, project_id=project_id,
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
            metadata=metadata, project_id=project_id,
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
    project_id: Any = _PROJECT_ID_UNSET,
) -> Dict[str, Any]:
    """Idempotent dispatch transition: no-op if already at or beyond target state.

    ``project_id`` scopes every step — the read, the no-op event, the UPDATE,
    the returned row and the event of the real transition (ADR-049). Not passing
    it keeps the pre-ADR-049 dispatch_id-only behaviour; a ``project_id`` that is
    ``None``, empty or not a string is refused.
    """
    scope = _resolve_project_scope(project_id)
    row = _dispatch_row(conn, dispatch_id, scope)
    if row is None:
        raise _dispatch_not_found(dispatch_id, scope)

    from_state = row["state"]
    noop = _check_idempotent_noop(
        conn, dispatch_id=dispatch_id, from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata, row=row, project_id=scope,
    )
    if noop is not None:
        return noop
    return _transition_dispatch_scoped(
        conn, dispatch_id=dispatch_id, to_state=to_state,
        actor=actor, reason=reason, metadata=metadata, project_id=scope,
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
    project_id: str,
    terminal_id: str,
    attempt_number: int,
    metadata: Optional[Dict[str, Any]] = None,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Create a new dispatch attempt record.

    ADR-007: project_id is MANDATORY — no silent default, mirroring
    register_dispatch(). Stamped on the INSERT row when the column exists
    (mirrors register_dispatch's own _has_project_id_col guard); older stores
    without the column are inserted exactly as before. OI-1625: the live
    dispatch_attempts.project_id column is NOT NULL with no DEFAULT (post
    migration 0031's adaptive-repair shape), so every prior INSERT here
    raised sqlite3.IntegrityError and dispatch_attempts stayed permanently
    empty despite 801+ rows in dispatches.
    """
    has_pid = _has_project_id_col(conn, "dispatch_attempts")
    attempt_id = _new_event_id()
    now = _now_utc()
    if has_pid:
        conn.execute(
            """
            INSERT INTO dispatch_attempts
                (attempt_id, dispatch_id, project_id, attempt_number, terminal_id, state, started_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (attempt_id, dispatch_id, project_id, attempt_number, terminal_id, now, _dump(metadata)),
        )
    else:
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
        project_id=project_id,
    )
    return dict(
        conn.execute("SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    )


def claim_next_queued_dispatch(
    conn: sqlite3.Connection,
    terminal_id: str,
    project_id: str,
    actor: str = "runtime",
) -> Optional[str]:
    """Atomically claim the next queued dispatch for a project.

    Uses BEGIN IMMEDIATE to serialize concurrent claimers — SQLite has no
    SKIP LOCKED; this ensures the SELECT→UPDATE is atomic across writers.
    Losers serialize and re-query; they get the next row or None.
    Returns the dispatch_id string if a dispatch was claimed, None if the
    queue is empty for this project.

    ADR-007: scoped to project_id — a claimer for project A NEVER sees
    project B queued rows. project_id is MANDATORY on every access.
    See docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md.

    Callers must pass a connection with no uncommitted DML. Setting
    isolation_level=None (required for explicit BEGIN IMMEDIATE) commits
    any pending implicit transaction on the connection.
    """
    prev_isolation_level = conn.isolation_level
    conn.isolation_level = None  # autocommit mode for manual BEGIN IMMEDIATE control
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT dispatch_id FROM dispatches
            WHERE project_id = ? AND state = 'queued'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return None

        dispatch_id = row["dispatch_id"]
        now = _now_utc()

        # State transition: queued → claimed — composite key (ADR-007: never dispatch_id alone)
        validate_dispatch_transition("queued", "claimed")
        conn.execute(
            "UPDATE dispatches SET state = 'claimed', updated_at = ? WHERE dispatch_id = ? AND project_id = ?",
            (now, dispatch_id, project_id),
        )
        _append_event(
            conn, event_type="dispatch_claimed", entity_type="dispatch",
            entity_id=dispatch_id, from_state="queued", to_state="claimed",
            actor=actor, reason=f"queue claim by terminal {terminal_id}",
            metadata={"terminal_id": terminal_id, "project_id": project_id},
            project_id=project_id,
        )

        # Provenance columns: claimed_by / claimed_at (ADR-007: composite key; ADR-005: audit event)
        table_cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatches)").fetchall()}
        if "claimed_by" in table_cols:
            conn.execute(
                """
                UPDATE dispatches
                SET terminal_id = ?, claimed_by = ?, claimed_at = ?
                WHERE dispatch_id = ? AND project_id = ?
                """,
                (terminal_id, terminal_id, now, dispatch_id, project_id),
            )
        else:
            conn.execute(
                "UPDATE dispatches SET terminal_id = ? WHERE dispatch_id = ? AND project_id = ?",
                (terminal_id, dispatch_id, project_id),
            )
        # ADR-005: ledger event for claimed_by/claimed_at provenance mutation
        _append_event(
            conn, event_type="dispatch_claim_provenance", entity_type="dispatch",
            entity_id=dispatch_id, from_state=None, to_state=None,
            actor=actor, reason=f"claim provenance stamped by terminal {terminal_id}",
            metadata={
                "terminal_id": terminal_id, "claimed_by": terminal_id,
                "claimed_at": now, "dispatch_id": dispatch_id, "project_id": project_id,
            },
            project_id=project_id,
        )

        conn.execute("COMMIT")
        return dispatch_id

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        # Restore the caller's isolation mode — don't leave it in autocommit. (OI-015)
        conn.isolation_level = prev_isolation_level


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
