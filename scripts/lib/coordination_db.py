#!/usr/bin/env python3
"""VNX Coordination DB — state types, DB connection, schema, raw queries. Leaf module."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

DB_FILENAME = "runtime_coordination.db"

# ---------------------------------------------------------------------------
# Canonical state enumerations
# ---------------------------------------------------------------------------

DISPATCH_STATES = frozenset({
    "queued", "claimed", "delivering", "accepted", "running",
    "completed", "timed_out", "failed_delivery", "expired", "recovered", "dead_letter",
})
TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter"})
ACCEPTED_OR_BEYOND_STATES = frozenset({
    "accepted", "running", "completed", "timed_out", "expired", "dead_letter",
})
LEASE_STATES = frozenset({"idle", "leased", "expired", "recovering", "released"})

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
LEASE_TRANSITIONS: Dict[str, frozenset] = {
    "idle":       frozenset({"leased"}),
    "leased":     frozenset({"released", "expired"}),
    "expired":    frozenset({"recovering", "idle"}),
    "recovering": frozenset({"idle", "leased"}),
    "released":   frozenset({"idle"}),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidStateError(ValueError):
    """Raised when a state value is not in the canonical set."""


class InvalidTransitionError(ValueError):
    """Raised when a state transition is not permitted."""


class DuplicateTransitionError(InvalidTransitionError):
    """Raised when a no-op transition is attempted on a terminal state.

    Attributes: dispatch_id, current_state, requested_state.
    """

    def __init__(self, message: str, *, dispatch_id: str = "",
                 current_state: str = "", requested_state: str = "") -> None:
        super().__init__(message)
        self.dispatch_id = dispatch_id
        self.current_state = current_state
        self.requested_state = requested_state


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
        (event_id, event_type, entity_type, entity_id, from_state, to_state,
         actor, reason, _dump(metadata), _now_utc()),
    )
    return event_id


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_schema(state_dir: str | Path, schema_sql_path: Optional[Path] = None) -> None:
    """Initialize (or migrate) the runtime coordination database. Idempotent."""
    if schema_sql_path is None:
        here = Path(__file__).resolve()
        schema_sql_path = here.parent.parent.parent / "schemas" / "runtime_coordination.sql"

    if not schema_sql_path.exists():
        raise FileNotFoundError(f"Runtime coordination schema not found: {schema_sql_path}")

    schema_sql = schema_sql_path.read_text(encoding="utf-8")

    with get_connection(state_dir) as conn:
        conn.executescript(schema_sql)
        conn.commit()

    current_version = 1
    with get_connection(state_dir) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM runtime_schema_version"
            ).fetchone()
            if row and row[0] is not None:
                current_version = row[0]
        except sqlite3.OperationalError:
            pass

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
) -> List[Dict[str, Any]]:
    """Return recent coordination events, newest first."""
    clauses: List[str] = []
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
    """Project current terminal_leases state into terminal_state.json format."""
    rows = conn.execute("SELECT * FROM terminal_leases").fetchall()

    terminals: Dict[str, Any] = {}
    for row in rows:
        r = dict(row)
        tid = r["terminal_id"]
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
