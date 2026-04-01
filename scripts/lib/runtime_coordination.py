#!/usr/bin/env python3
"""
VNX Runtime Coordination — Python helpers for state transitions and events.

Canonical source of truth for:
  - Dispatch states and valid transitions
  - Lease states and valid transitions
  - Coordination event appends
  - Database connection management

IMPORTANT: terminal_state.json and panes.json are DERIVED PROJECTIONS.
  - terminal_state.json reflects the current terminal_leases table.
  - panes.json provides tmux adapter mappings only; it is not ownership truth.
  Do not treat either file as authoritative for lease or dispatch state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Optional

# ---------------------------------------------------------------------------
# Canonical state enumerations
# ---------------------------------------------------------------------------

DISPATCH_STATES = frozenset({
    "queued",
    "claimed",
    "delivering",
    "accepted",
    "running",
    "completed",
    "timed_out",
    "failed_delivery",
    "expired",
    "recovered",
    "dead_letter",
})

# Terminal dispatch states: no outgoing transitions permitted.
TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter"})

# States that indicate acceptance has already occurred (accepted or beyond).
ACCEPTED_OR_BEYOND_STATES = frozenset({
    "accepted", "running", "completed", "timed_out", "expired", "dead_letter",
})

LEASE_STATES = frozenset({
    "idle",
    "leased",
    "expired",
    "recovering",
    "released",
})

# Valid dispatch state transitions: {from_state: set_of_allowed_to_states}
DISPATCH_TRANSITIONS: Dict[str, frozenset] = {
    "queued":          frozenset({"claimed", "expired"}),
    "claimed":         frozenset({"delivering", "expired", "recovered"}),
    "delivering":      frozenset({"accepted", "failed_delivery", "timed_out"}),
    "accepted":        frozenset({"running", "timed_out"}),
    "running":         frozenset({"completed", "timed_out", "failed_delivery"}),
    "completed":       frozenset(),
    "timed_out":       frozenset({"recovered", "expired", "dead_letter"}),
    "failed_delivery": frozenset({"recovered", "expired", "dead_letter"}),
    "expired":         frozenset(),
    "recovered":       frozenset({"queued", "claimed", "expired", "dead_letter"}),
    "dead_letter":     frozenset(),
}

# Valid lease state transitions: {from_state: set_of_allowed_to_states}
LEASE_TRANSITIONS: Dict[str, frozenset] = {
    "idle":       frozenset({"leased"}),
    "leased":     frozenset({"released", "expired"}),
    "expired":    frozenset({"recovering", "idle"}),
    "recovering": frozenset({"idle", "leased"}),
    "released":   frozenset({"idle"}),
}

DB_FILENAME = "runtime_coordination.db"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class InvalidStateError(ValueError):
    """Raised when a state value is not in the canonical set."""


class InvalidTransitionError(ValueError):
    """Raised when a state transition is not permitted."""


class DuplicateTransitionError(InvalidTransitionError):
    """Raised when a transition is a no-op because the target state was already reached.

    This is a subclass of InvalidTransitionError so existing catch blocks
    continue to work, but callers that want idempotent behavior can catch
    this specifically and treat it as a safe no-op.

    Attributes:
        dispatch_id: The dispatch that was already in the target state.
        current_state: The state the dispatch is currently in.
        requested_state: The state that was requested.
    """

    def __init__(
        self,
        message: str,
        *,
        dispatch_id: str = "",
        current_state: str = "",
        requested_state: str = "",
    ) -> None:
        super().__init__(message)
        self.dispatch_id = dispatch_id
        self.current_state = current_state
        self.requested_state = requested_state


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


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def db_path_from_state_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / DB_FILENAME


@contextmanager
def get_connection(
    state_dir: str | Path,
    *,
    timeout: float = 10.0,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a WAL-mode SQLite connection with FK enforcement."""
    path = db_path_from_state_dir(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _dump(obj: Any) -> str:
    return json.dumps(obj) if obj is not None else "{}"


def _append_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Append a coordination event row. Returns the event_id."""
    event_id = _new_event_id()
    conn.execute(
        """
        INSERT INTO coordination_events
            (event_id, event_type, entity_type, entity_id,
             from_state, to_state, actor, reason, metadata_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            entity_type,
            entity_id,
            from_state,
            to_state,
            actor,
            reason,
            _dump(metadata),
            _now_utc(),
        ),
    )
    return event_id


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_schema(state_dir: str | Path, schema_sql_path: Optional[Path] = None) -> None:
    """Initialize (or migrate) the runtime coordination database.

    Idempotent: safe to call multiple times. Uses CREATE TABLE IF NOT EXISTS
    and INSERT OR IGNORE throughout — no destructive operations.

    Applies all available schema migrations in order (v1, v2, ...).

    Args:
        state_dir: Directory where runtime_coordination.db lives.
        schema_sql_path: Path to runtime_coordination.sql. Defaults to
            schemas/runtime_coordination.sql relative to VNX_HOME.
    """
    if schema_sql_path is None:
        # Resolve relative to this file: scripts/lib/ -> scripts/ -> VNX_HOME
        here = Path(__file__).resolve()
        schema_sql_path = here.parent.parent.parent / "schemas" / "runtime_coordination.sql"

    if not schema_sql_path.exists():
        raise FileNotFoundError(f"Runtime coordination schema not found: {schema_sql_path}")

    schema_sql = schema_sql_path.read_text(encoding="utf-8")

    with get_connection(state_dir) as conn:
        conn.executescript(schema_sql)
        conn.commit()

    # Determine the current schema version to skip already-applied migrations.
    # This is critical for idempotency: migrations that use ALTER TABLE ADD COLUMN
    # (e.g., v4) will fail on re-run because SQLite does not support
    # ALTER TABLE ADD COLUMN IF NOT EXISTS.
    current_version = 1
    with get_connection(state_dir) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM runtime_schema_version"
            ).fetchone()
            if row and row[0] is not None:
                current_version = row[0]
        except sqlite3.OperationalError:
            pass  # Table may not exist yet on first init

    # Apply incremental migrations (v2, v3, ...) if available
    schemas_dir = schema_sql_path.parent
    version = 2
    while True:
        migration = schemas_dir / f"runtime_coordination_v{version}.sql"
        if not migration.exists():
            break
        if version <= current_version:
            version += 1
            continue
        migration_sql = migration.read_text(encoding="utf-8")
        with get_connection(state_dir) as conn:
            conn.executescript(migration_sql)
            conn.commit()
        version += 1


# ---------------------------------------------------------------------------
# Dispatch operations
# ---------------------------------------------------------------------------

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
    """Register a new dispatch in the queued state.

    Idempotent: if dispatch_id already exists, returns the existing record
    without modifying it (respects G-R6: dispatch bundles are immutable after send).

    Returns the dispatch row as a dict.
    """
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
        (
            dispatch_id,
            terminal_id,
            track,
            priority,
            pr_ref,
            gate,
            bundle_path,
            expires_after,
            now,
            now,
            _dump(metadata),
        ),
    )

    _append_event(
        conn,
        event_type="dispatch_queued",
        entity_type="dispatch",
        entity_id=dispatch_id,
        from_state=None,
        to_state="queued",
        actor=actor,
        reason="initial registration",
        metadata=metadata,
    )

    return dict(
        conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
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
    """Transition a dispatch to a new state.

    Validates the transition against DISPATCH_TRANSITIONS, updates the row,
    and appends a coordination event.

    Returns the updated dispatch row as a dict.
    Raises KeyError if dispatch_id not found.
    Raises InvalidTransitionError if the transition is not permitted.
    """
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

    event_type = f"dispatch_{to_state}"
    _append_event(
        conn,
        event_type=event_type,
        entity_type="dispatch",
        entity_id=dispatch_id,
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        reason=reason,
        metadata=metadata,
    )

    return dict(
        conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
    )


def transition_dispatch_idempotent(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    to_state: str,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Idempotent dispatch transition: no-op if already at or beyond target state.

    Unlike transition_dispatch, this function does NOT raise for duplicate
    transitions. Instead it:
      - Returns the current row unchanged if the dispatch is already in
        to_state or has progressed past it.
      - Appends a 'dispatch_noop' coordination event for audit visibility.
      - Raises DuplicateTransitionError only for terminal states where
        re-acceptance is explicitly rejected (completed, expired, dead_letter).

    For valid forward transitions, behaves identically to transition_dispatch.

    Returns the dispatch row as a dict (possibly unchanged for no-ops).
    Raises KeyError if dispatch_id not found.
    Raises DuplicateTransitionError if dispatch is in a terminal state.
    Raises InvalidTransitionError for genuinely invalid transitions.
    """
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Dispatch not found: {dispatch_id!r}")

    from_state = row["state"]

    # Already in the requested state — pure no-op
    if from_state == to_state:
        _append_event(
            conn,
            event_type="dispatch_noop",
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason or f"idempotent no-op: already in {to_state!r}",
            metadata=metadata,
        )
        return dict(row)

    # Terminal state — reject explicitly
    if is_terminal_dispatch_state(from_state):
        raise DuplicateTransitionError(
            f"Dispatch {dispatch_id!r} is in terminal state {from_state!r}; "
            f"cannot transition to {to_state!r}",
            dispatch_id=dispatch_id,
            current_state=from_state,
            requested_state=to_state,
        )

    # Already past the requested state (e.g., requesting 'accepted' but already 'running')
    if to_state == "accepted" and is_accepted_or_beyond(from_state):
        _append_event(
            conn,
            event_type="dispatch_noop",
            entity_type="dispatch",
            entity_id=dispatch_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason or f"idempotent no-op: already at {from_state!r} (past {to_state!r})",
            metadata=metadata,
        )
        return dict(row)

    # Valid forward transition — delegate to the strict version
    return transition_dispatch(
        conn,
        dispatch_id=dispatch_id,
        to_state=to_state,
        actor=actor,
        reason=reason,
        metadata=metadata,
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


# ---------------------------------------------------------------------------
# Dispatch attempt operations
# ---------------------------------------------------------------------------

def create_attempt(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    terminal_id: str,
    attempt_number: int,
    metadata: Optional[Dict[str, Any]] = None,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Create a new dispatch attempt record.

    Returns the inserted attempt row as a dict.
    """
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
        conn,
        event_type="attempt_created",
        entity_type="attempt",
        entity_id=attempt_id,
        from_state=None,
        to_state="pending",
        actor=actor,
        reason=f"attempt {attempt_number} for dispatch {dispatch_id}",
        metadata={"dispatch_id": dispatch_id, "terminal_id": terminal_id, "attempt_number": attempt_number},
    )

    return dict(
        conn.execute(
            "SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
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
    """Update a dispatch attempt state and optionally record failure reason.

    Returns the updated attempt row as a dict.
    """
    row = conn.execute(
        "SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Attempt not found: {attempt_id!r}")

    from_state = row["state"]
    now = _now_utc()
    conn.execute(
        """
        UPDATE dispatch_attempts
        SET state = ?, ended_at = ?, failure_reason = ?
        WHERE attempt_id = ?
        """,
        (state, now, failure_reason, attempt_id),
    )

    event_type = "attempt_failed" if state == "failed" else f"attempt_{state}"
    _append_event(
        conn,
        event_type=event_type,
        entity_type="attempt",
        entity_id=attempt_id,
        from_state=from_state,
        to_state=state,
        actor=actor,
        reason=failure_reason,
        metadata=metadata,
    )

    return dict(
        conn.execute(
            "SELECT * FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# Lease operations
# ---------------------------------------------------------------------------

def _default_expires(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    dispatch_id: str,
    lease_seconds: int = 600,
    actor: str = "runtime",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Acquire a lease for a terminal.

    Raises InvalidTransitionError if terminal is not in idle state.
    Returns updated lease row as a dict.
    """
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal lease record not found for: {terminal_id!r}. "
                       f"Was the schema initialized?")

    from_state = row["state"]
    validate_lease_transition(from_state, "leased")

    now = _now_utc()
    new_generation = row["generation"] + 1
    expires_at = _default_expires(lease_seconds)

    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'leased', dispatch_id = ?, generation = ?,
            leased_at = ?, expires_at = ?, last_heartbeat_at = ?,
            released_at = NULL
        WHERE terminal_id = ?
        """,
        (dispatch_id, new_generation, now, expires_at, now, terminal_id),
    )

    _append_event(
        conn,
        event_type="lease_acquired",
        entity_type="lease",
        entity_id=terminal_id,
        from_state=from_state,
        to_state="leased",
        actor=actor,
        reason=reason or f"dispatch {dispatch_id}",
        metadata={"dispatch_id": dispatch_id, "generation": new_generation, "expires_at": expires_at},
    )

    return dict(
        conn.execute(
            "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
    )


def renew_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    generation: int,
    lease_seconds: int = 600,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Renew a lease heartbeat. Generation must match to prevent stale renewal.

    Returns updated lease row as a dict.
    Raises ValueError if generation mismatch.
    """
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    if row["state"] != "leased":
        raise InvalidTransitionError(
            f"Cannot renew lease for {terminal_id!r}: current state is {row['state']!r}, expected 'leased'"
        )
    if row["generation"] != generation:
        raise ValueError(
            f"Lease generation mismatch for {terminal_id!r}: "
            f"provided {generation}, current {row['generation']}. Stale renewal rejected."
        )

    now = _now_utc()
    expires_at = _default_expires(lease_seconds)

    conn.execute(
        """
        UPDATE terminal_leases
        SET expires_at = ?, last_heartbeat_at = ?
        WHERE terminal_id = ?
        """,
        (expires_at, now, terminal_id),
    )

    _append_event(
        conn,
        event_type="lease_renewed",
        entity_type="lease",
        entity_id=terminal_id,
        from_state="leased",
        to_state="leased",
        actor=actor,
        metadata={"generation": generation, "new_expires_at": expires_at},
    )

    return dict(
        conn.execute(
            "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
    )


def release_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    generation: int,
    actor: str = "runtime",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Release a lease, returning terminal to idle.

    Generation must match (G-R3: stale reclaim must be auditable).
    Returns updated lease row as a dict.
    """
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    if from_state not in ("leased", "recovering"):
        raise InvalidTransitionError(
            f"Cannot release lease for {terminal_id!r}: current state is {from_state!r}"
        )
    if row["generation"] != generation:
        raise ValueError(
            f"Lease generation mismatch for {terminal_id!r}: "
            f"provided {generation}, current {row['generation']}. Stale release rejected."
        )

    now = _now_utc()
    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'released', dispatch_id = NULL,
            expires_at = NULL, released_at = ?
        WHERE terminal_id = ?
        """,
        (now, terminal_id),
    )

    _append_event(
        conn,
        event_type="lease_released",
        entity_type="lease",
        entity_id=terminal_id,
        from_state=from_state,
        to_state="released",
        actor=actor,
        reason=reason,
        metadata={"generation": generation},
    )

    # Immediately move released -> idle (released is a transient state)
    conn.execute(
        "UPDATE terminal_leases SET state = 'idle' WHERE terminal_id = ?",
        (terminal_id,),
    )
    _append_event(
        conn,
        event_type="lease_returned_idle",
        entity_type="lease",
        entity_id=terminal_id,
        from_state="released",
        to_state="idle",
        actor=actor,
    )

    return dict(
        conn.execute(
            "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
    )


def expire_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    actor: str = "reconciler",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a lease as expired (used by reconciler for TTL enforcement).

    Returns updated lease row as a dict.
    """
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    validate_lease_transition(from_state, "expired")

    conn.execute(
        "UPDATE terminal_leases SET state = 'expired' WHERE terminal_id = ?",
        (terminal_id,),
    )

    _append_event(
        conn,
        event_type="lease_expired",
        entity_type="lease",
        entity_id=terminal_id,
        from_state=from_state,
        to_state="expired",
        actor=actor,
        reason=reason or "TTL elapsed",
        metadata={"dispatch_id": row["dispatch_id"], "generation": row["generation"]},
    )

    return dict(
        conn.execute(
            "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
    )


def recover_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    actor: str = "reconciler",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Transition an expired lease to recovering, then idle.

    Returns updated lease row as a dict.
    """
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    validate_lease_transition(from_state, "recovering")

    conn.execute(
        "UPDATE terminal_leases SET state = 'recovering' WHERE terminal_id = ?",
        (terminal_id,),
    )

    _append_event(
        conn,
        event_type="lease_recovering",
        entity_type="lease",
        entity_id=terminal_id,
        from_state=from_state,
        to_state="recovering",
        actor=actor,
        reason=reason or "expired lease recovery",
        metadata={"dispatch_id": row["dispatch_id"], "generation": row["generation"]},
    )

    # Complete recovery: move to idle, clear dispatch linkage
    now = _now_utc()
    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'idle', dispatch_id = NULL, expires_at = NULL,
            leased_at = NULL, released_at = ?
        WHERE terminal_id = ?
        """,
        (now, terminal_id),
    )
    _append_event(
        conn,
        event_type="lease_recovered",
        entity_type="lease",
        entity_id=terminal_id,
        from_state="recovering",
        to_state="idle",
        actor=actor,
        reason=reason,
    )

    return dict(
        conn.execute(
            "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_dispatch(conn: sqlite3.Connection, dispatch_id: str) -> Optional[Dict[str, Any]]:
    """Return dispatch row or None."""
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    return dict(row) if row else None


def get_lease(conn: sqlite3.Connection, terminal_id: str) -> Optional[Dict[str, Any]]:
    """Return terminal lease row or None."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    return dict(row) if row else None


def get_events(
    conn: sqlite3.Connection,
    *,
    entity_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> list:
    """Return recent coordination events, newest first."""
    clauses = []
    params: list = []
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM coordination_events {where} ORDER BY occurred_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def project_terminal_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Project current terminal_leases state into terminal_state.json format.

    This is the canonical projection function. terminal_state.json should
    be written by calling this function and serializing the result —
    never derived from tmux pane state or other sources.

    Returns a dict compatible with the terminal_state.json schema.
    """
    rows = conn.execute("SELECT * FROM terminal_leases").fetchall()

    terminals: Dict[str, Any] = {}
    for row in rows:
        r = dict(row)
        tid = r["terminal_id"]
        # Map lease states to terminal_state.json status conventions
        lease_state = r["state"]
        if lease_state == "leased":
            status = "working"
        elif lease_state in ("expired", "recovering"):
            status = "recovering"
        else:
            status = "idle"

        record: Dict[str, Any] = {
            "terminal_id": tid,
            "status": status,
            "version": r["generation"],
        }
        if r.get("dispatch_id"):
            record["claimed_by"] = r["dispatch_id"]
        if r.get("leased_at"):
            record["claimed_at"] = r["leased_at"]
        if r.get("expires_at"):
            record["lease_expires_at"] = r["expires_at"]
        if r.get("last_heartbeat_at"):
            record["last_activity"] = r["last_heartbeat_at"]

        terminals[tid] = record

    return {"schema_version": 1, "terminals": terminals}


def release_all_leases(
    conn: sqlite3.Connection,
    *,
    actor: str = "chain_closeout",
    reason: str = "chain_boundary_cleanup",
    force: bool = False,
) -> Dict[str, Any]:
    """Release all non-idle terminal leases to idle state at chain boundary.

    BOOT-9: Releases ALL terminal leases regardless of current state.
    BOOT-10: Follows verify -> release -> audit -> confirm sequence.
    BOOT-11: Increments generation to guard against stale delayed releases from
             the old chain (a delayed release-on-failure using the old generation
             will be rejected by the generation guard in release_lease).

    Args:
        conn: Open database connection. Caller must commit.
        actor: Actor recorded in audit events (default: 'chain_closeout').
        reason: Reason recorded in audit events.
        force: If True, proceed even when non-terminal dispatches exist.

    Returns dict with keys:
        released: list of terminal_ids that were released.
        already_idle: list of terminal_ids already idle.
        non_terminal_dispatches: list of {dispatch_id, state} for non-terminal dispatches.
        blocked: True if blocked by non-terminal dispatches (force=False).
        all_idle: True if all leases are now idle.
        error: Present when post-release verification fails.
    """
    # BOOT-10 Step 1: VERIFY — check for non-terminal dispatches.
    non_terminal_rows = conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE state NOT IN (?, ?, ?)",
        ("completed", "expired", "dead_letter"),
    ).fetchall()
    non_terminal = [dict(r) for r in non_terminal_rows]

    if non_terminal and not force:
        return {
            "released": [],
            "already_idle": [],
            "non_terminal_dispatches": non_terminal,
            "blocked": True,
            "all_idle": False,
            "message": (
                f"WARN: {len(non_terminal)} non-terminal dispatch(es) exist. "
                "Use --force to proceed with lease cleanup."
            ),
        }

    now = _now_utc()
    released = []
    already_idle = []

    lease_rows = conn.execute("SELECT * FROM terminal_leases").fetchall()

    for row in lease_rows:
        terminal_id = row["terminal_id"]
        old_state = row["state"]
        old_generation = row["generation"]

        if old_state == "idle":
            already_idle.append(terminal_id)
            continue

        # BOOT-10 Step 2: RELEASE — set directly to idle with generation increment.
        # BOOT-11: new_generation = generation + 1 invalidates any in-flight
        #          release-on-failure calls from the old chain.
        new_generation = old_generation + 1
        conn.execute(
            """
            UPDATE terminal_leases
            SET state = 'idle',
                dispatch_id = NULL,
                leased_at = NULL,
                expires_at = NULL,
                last_heartbeat_at = NULL,
                released_at = ?,
                generation = ?
            WHERE terminal_id = ?
            """,
            (now, new_generation, terminal_id),
        )

        # BOOT-10 Step 3: AUDIT — emit coordination events for each released lease.
        _append_event(
            conn,
            event_type="lease_released",
            entity_type="lease",
            entity_id=terminal_id,
            from_state=old_state,
            to_state="idle",
            actor=actor,
            reason=reason,
            metadata={
                "generation": old_generation,
                "new_generation": new_generation,
                "dispatch_id": row["dispatch_id"],
            },
        )
        released.append(terminal_id)

    # BOOT-10 Step 4: VERIFY confirmation — abort if any non-idle lease remains.
    remaining_rows = conn.execute(
        "SELECT terminal_id, state FROM terminal_leases WHERE state != 'idle'"
    ).fetchall()

    if remaining_rows:
        remaining = [dict(r) for r in remaining_rows]
        return {
            "released": released,
            "already_idle": already_idle,
            "non_terminal_dispatches": non_terminal,
            "blocked": False,
            "all_idle": False,
            "error": (
                f"Verification failed: {len(remaining)} non-idle lease(s) remain "
                "after closeout cleanup"
            ),
            "remaining": remaining,
        }

    return {
        "released": released,
        "already_idle": already_idle,
        "non_terminal_dispatches": non_terminal,
        "blocked": False,
        "all_idle": True,
    }
