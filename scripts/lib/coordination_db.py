#!/usr/bin/env python3
"""VNX Coordination DB — state types, DB connection, schema, raw queries. Leaf module."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import schema_migration

try:
    from project_scope import TenantUnresolved
    from project_scope import current_project_id as _current_project_id
    from project_scope import project_filter_enabled as _project_filter_enabled
    from project_scope import resolve_stamp_project_id as _resolve_stamp_project_id
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from project_scope import TenantUnresolved  # type: ignore[no-redef]
    from project_scope import current_project_id as _current_project_id  # type: ignore[no-redef]
    from project_scope import project_filter_enabled as _project_filter_enabled  # type: ignore[no-redef]
    from project_scope import resolve_stamp_project_id as _resolve_stamp_project_id  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

DB_FILENAME = "runtime_coordination.db"

# ---------------------------------------------------------------------------
# Canonical state enumerations
# ---------------------------------------------------------------------------

DISPATCH_STATES = frozenset({
    "proposed", "ready",
    "queued", "claimed", "delivering", "accepted", "running",
    "completed", "timed_out", "failed_delivery", "expired", "recovered", "dead_letter",
    "withdrawn",
})
# The ONE terminal set (ADR-049): track_reconciler and runtime_supervisor read it.
# withdrawn ends a deliverable that was never built; it is neither delivered nor failed.
TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter", "withdrawn"})
ACCEPTED_OR_BEYOND_STATES = frozenset({
    "accepted", "running", "completed", "timed_out", "expired", "dead_letter",
})
LEASE_STATES = frozenset({"idle", "leased", "expired", "recovering", "released"})

DISPATCH_TRANSITIONS: Dict[str, frozenset] = {
    # Planning gate: proposed (un-approved) -> ready (operator-approved)
    "proposed":        frozenset({"ready", "withdrawn"}),
    # Execution gate: ready -> claimed (dispatchable once promoted)
    "ready":           frozenset({"claimed", "expired", "withdrawn"}),
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
    "withdrawn":       frozenset(),
}
# States that have not started yet: planning gate and the queue.
PRE_FLIGHT_DISPATCH_STATES = frozenset({"proposed", "ready", "queued"})
# A dispatch is in flight between claim and completion. Derived, not listed:
# everything that is neither terminal nor pre-flight, minus the recovery
# branch (a state that can still be dead-lettered is a failure waiting for
# recovery, not live work). A new state on the happy path joins by itself.
IN_FLIGHT_DISPATCH_STATES = frozenset(
    s for s in DISPATCH_STATES - TERMINAL_DISPATCH_STATES - PRE_FLIGHT_DISPATCH_STATES
    if "dead_letter" not in DISPATCH_TRANSITIONS[s]
)
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


@contextmanager
def get_connection_for_db(
    db_path: str | Path,
    *,
    timeout: float = 10.0,
) -> Generator[sqlite3.Connection, None, None]:
    """Like get_connection but accepts a direct DB file path instead of state_dir."""
    path = Path(db_path)
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


def stamp_project_id(conn: sqlite3.Connection) -> str:
    """The project id an INSERT stamps on a row of the store *conn* is open on.

    Resolved from the store's own file, with the resolver migration 0034 uses
    for the column default (``project_scope.resolve_stamp_project_id`` →
    ``project_id_migration.resolve_init_project_id(strict=True)``): DB-path
    layout, ``.vnx-project-id`` marker and ``VNX_PROJECT_ID`` must agree. So an
    explicit stamp and the default never name two projects.

    Raises ``TenantUnresolved`` when nothing resolves, when the sources
    disagree, or when the main database has no file. Never a guessed default: on
    the sales-copilot shape project_id is NOT NULL without a default, so a
    writer has to supply it (OI-2026).
    """
    path = ""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            path = row[2] or ""
            break
    if not path:
        raise TenantUnresolved(
            "stamp_project_id: the connection's main database has no file, so it "
            "is no project store; refusing to stamp a guessed project_id"
        )
    return _resolve_stamp_project_id(db_path=path)


@dataclass(frozen=True)
class ProjectIdStamp:
    """The project_id part of an INSERT: ``column`` and ``placeholder`` go at the
    end of the column list and the VALUES list, ``values`` at the end of the
    parameters. All three are empty for a table without the column."""

    column: str
    placeholder: str
    values: Tuple[str, ...]


def project_id_stamp(conn: sqlite3.Connection, table: str) -> ProjectIdStamp:
    """How an INSERT into *table* carries project_id on the store *conn* is open on.

    A table with the column gets the store's id from :func:`stamp_project_id`,
    explicitly: the sales-copilot shape has no default to fall back on. A table
    without it (a store before migration 0034, or one init_schema built without
    the numbered migrations) gets nothing, and the INSERT keeps the column list
    it had before 0034.
    """
    if not _has_project_id_col(conn, table):
        return ProjectIdStamp("", "", ())
    return ProjectIdStamp(", project_id", ", ?", (stamp_project_id(conn),))


def upsert_conflict_target(
    conn: sqlite3.Connection, table: str, natural_key: Tuple[str, ...]
) -> str:
    """The ON CONFLICT target of an upsert on *table* keyed by *natural_key*, as
    the store *conn* is open on carries it (OI-2026).

    SQLite accepts a conflict target only when a full (not partial) UNIQUE index
    or PRIMARY KEY covers exactly its columns, in any order. The store carries
    one of two keys:
      - ``(project_id, *natural_key)``: migration 0034 creates it wherever the
        table has project_id, and the sales-copilot shape and tenant stamping
        build it into the table itself. It wins when present: a row of another
        project then fails on a single-store key instead of updating that
        project's row;
      - *natural_key* alone: a store before 0034, with or without the column.
    Raises sqlite3.OperationalError naming the table when the store carries
    neither, which is what the statement itself would raise.
    """
    keys = set()
    for row in conn.execute(f"PRAGMA index_list('{table}')").fetchall():
        if row[2] and not row[4]:
            keys.add(frozenset(r[2] for r in conn.execute(f"PRAGMA index_info('{row[1]}')")))
    for candidate in (("project_id", *natural_key), tuple(natural_key)):
        if frozenset(candidate) in keys:
            return ", ".join(candidate)
    raise sqlite3.OperationalError(
        f"upsert_conflict_target: {table} carries no full UNIQUE index over "
        f"{natural_key}, with or without project_id"
    )


def _has_project_id_col(conn: sqlite3.Connection, table: str) -> bool:
    """True if `table` has a project_id column (post-migration-0010)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(
        (row[1] if not isinstance(row, sqlite3.Row) else row["name"]) == "project_id"
        for row in rows
    )


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
    project_id: Optional[str] = None,
) -> str:
    """Append a coordination event row. Returns the event_id."""
    event_id = _new_event_id()
    if _has_project_id_col(conn, "coordination_events"):
        effective_pid = project_id or _current_project_id()
        conn.execute(
            """
            INSERT INTO coordination_events
                (event_id, event_type, entity_type, entity_id,
                 from_state, to_state, actor, reason, metadata_json, occurred_at,
                 project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, event_type, entity_type, entity_id, from_state, to_state,
             actor, reason, _dump(metadata), _now_utc(), effective_pid),
        )
    else:
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


def _needs_initial_migration(conn: sqlite3.Connection) -> bool:
    """Check if initial migration is needed.

    Modern path: PRAGMA user_version.
    Legacy fallback: runtime_schema_version table.
    """
    pragma_version = schema_migration.get_user_version(conn)
    if pragma_version >= 1:
        return False
    # Legacy fallback: check runtime_schema_version table
    try:
        row = conn.execute(
            "SELECT version FROM runtime_schema_version ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
        if row and row[0] >= 1:
            # Legacy install — sync PRAGMA user_version forward
            conn.execute(f"PRAGMA user_version = {row[0]}")
            return False
    except sqlite3.OperationalError as e:
        # Table doesn't exist → fresh install, needs migration
        logger.info("coordination_db: no runtime_schema_version table (%s) — assuming fresh install", e)
    return True


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

# ADR-007: composite UNIQUE indexes over project_id for RC tables that historically
# carried only a single-column PK/UNIQUE. Each entry is (table, [columns]).
# The last seven are the runtime tables of migration 0015's runtime partition; 0034
# gives them project_id where a store missed it and creates every index below (OI-2026).
_RC_COMPOSITE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("coordination_events", ("project_id", "id")),
    ("incident_log", ("project_id", "id")),
    ("intelligence_injections", ("project_id", "id")),
    ("worker_pool_membership", ("project_id", "id")),
    ("retry_budgets", ("project_id", "id")),
    ("retry_state", ("project_id", "id")),
    ("escalation_log", ("project_id", "id")),
    ("execution_targets", ("project_id", "id")),
    ("inbound_inbox", ("project_id", "id")),
    ("recommendations", ("project_id", "id")),
    ("recommendation_outcomes", ("project_id", "id")),
)

# Durable record of tables whose ux_<table>_pid index could not be created due to
# an UNEXPECTED (non-IntegrityError) sqlite3 error. user_version still advances
# (the migration is non-destructive and non-aborting), so this marker — plus
# composite_index_audit() — is what surfaces the under-enforcement (OI-563).
_COMPOSITE_KEY_FAILURES_TABLE = "composite_key_failures"


def _record_composite_index_failure(
    conn: sqlite3.Connection,
    table: str,
    cols: tuple[str, ...],
    error: sqlite3.Error,
) -> None:
    """Durably record an unexpected composite UNIQUE index creation failure.

    Upserts one row per table (latest error wins) into
    ``composite_key_failures``. Best-effort by design: the migration stays
    non-destructive even if the marker write itself fails — the original error
    is still ERROR-logged by the caller. Stale rows for tables that later get
    their index are filtered out by ``composite_index_audit``.
    """
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_COMPOSITE_KEY_FAILURES_TABLE} ("
            "table_name TEXT PRIMARY KEY, "
            "index_name TEXT NOT NULL, "
            "columns TEXT NOT NULL, "
            "error TEXT NOT NULL, "
            "occurred_at TEXT NOT NULL)"
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {_COMPOSITE_KEY_FAILURES_TABLE} "
            "(table_name, index_name, columns, error, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (table, f"ux_{table}_pid", ", ".join(cols), str(error), _now_utc()),
        )
    except sqlite3.Error as exc:
        logger.error(
            "composite-keys: failed to record durable failure marker for %s — %s",
            table, exc,
        )


def composite_index_audit(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Return ADR-007 composite UNIQUE index coverage for the coordination DB.

    A table counts as under-enforced when it exists in the schema but has no
    ``ux_<table>_pid`` index attached to it — either absent, or shadowed by an
    index of the same name on a different table (which ``CREATE UNIQUE INDEX IF
    NOT EXISTS`` silently accepts as a no-op). Durable failure markers recorded
    by ``_migrate_v11_composite_keys`` for unexpected per-table errors are
    surfaced for tables still missing their index, so past under-enforcement
    stays visible even after user_version has advanced (OI-563).

    Returns:
      {
        "missing":  [{"table", "index", "columns"}, ...],
        "failures": [{"table", "index", "columns", "error", "occurred_at"}, ...],
        "tables_checked": int,
      }
    """
    missing: List[Dict[str, str]] = []
    tables_checked = 0
    for table, cols in _RC_COMPOSITE_KEYS:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        tables_checked += 1
        index_name = f"ux_{table}_pid"
        row = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        owner = None if row is None else (
            row[0] if not isinstance(row, sqlite3.Row) else row["tbl_name"]
        )
        if owner != table:
            missing.append({"table": table, "index": index_name, "columns": ", ".join(cols)})

    failures: List[Dict[str, str]] = []
    missing_tables = {m["table"] for m in missing}
    try:
        marker_rows = conn.execute(
            f"SELECT table_name, index_name, columns, error, occurred_at "
            f"FROM {_COMPOSITE_KEY_FAILURES_TABLE} ORDER BY occurred_at DESC"
        ).fetchall()
    except sqlite3.Error:
        marker_rows = []
    for row in marker_rows:
        table_name = row[0] if not isinstance(row, sqlite3.Row) else row["table_name"]
        if table_name not in missing_tables:
            # Stale marker: the table has its index again — don't surface it.
            continue
        failures.append({
            "table": table_name,
            "index": row[1] if not isinstance(row, sqlite3.Row) else row["index_name"],
            "columns": row[2] if not isinstance(row, sqlite3.Row) else row["columns"],
            "error": row[3] if not isinstance(row, sqlite3.Row) else row["error"],
            "occurred_at": row[4] if not isinstance(row, sqlite3.Row) else row["occurred_at"],
        })

    return {
        "missing": missing,
        "failures": failures,
        "tables_checked": tables_checked,
    }


def _migrate_v11_composite_keys(conn: sqlite3.Connection) -> None:
    """V11: ADR-007 composite UNIQUE indexes over project_id (non-destructive).

    Creates ``CREATE UNIQUE INDEX IF NOT EXISTS ux_<table>_pid`` for each table
    listed in ``_RC_COMPOSITE_KEYS``. Each index creation is wrapped in its own
    try/except: a missing column or existing duplicate rows in a given table only
    skips that one table and logs a clear warning. The migration never aborts,
    never deletes rows, and never rewrites data.

    An UNEXPECTED per-table error (non-IntegrityError) is ERROR-logged and
    durably recorded via ``_record_composite_index_failure`` so the missing
    index — which user_version still advances past — surfaces through
    ``composite_index_audit()`` instead of being silently dropped (OI-563).
    """
    for table, cols in _RC_COMPOSITE_KEYS:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            logger.warning("composite-keys: skipped %s — table does not exist", table)
            continue

        actual_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = [c for c in cols if c not in actual_cols]
        if missing:
            logger.warning(
                "composite-keys: skipped %s — missing column(s): %s",
                table, ", ".join(missing)
            )
            continue

        index_name = f"ux_{table}_pid"
        col_list = ", ".join(cols)
        try:
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {table} ({col_list})"
            )
            logger.info(
                "composite-keys: created UNIQUE INDEX %s ON %s(%s) (ADR-007)",
                index_name, table, col_list
            )
        except sqlite3.IntegrityError as exc:
            # Expected: this store has duplicate (project_id, key) rows. Skip + surface
            # so a dedup can be done before the index is added — never abort, never delete.
            logger.warning(
                "composite-keys: skipped %s — duplicate rows for %s, dedup needed (%s)",
                table, col_list, exc,
            )
        except sqlite3.Error as exc:
            # Unexpected (e.g. a migration bug / bad column) — must NOT be swallowed as a
            # data-violation skip. Surface at ERROR level AND durably so it is investigated
            # even though user_version advances; still do not abort the whole migration run.
            logger.error(
                "composite-keys: UNEXPECTED error on %s — index NOT created; investigate (%s)",
                table, exc,
            )
            _record_composite_index_failure(conn, table, cols, exc)


def _migrate_v11_composite_keys_down(conn: sqlite3.Connection) -> None:
    """Down migration for V11: drop the ADR-007 composite UNIQUE indexes.

    Idempotent — ``DROP INDEX IF EXISTS`` is a no-op when the index is absent.
    """
    for table, _ in _RC_COMPOSITE_KEYS:
        index_name = f"ux_{table}_pid"
        try:
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            logger.info("composite-keys: dropped %s (V11 down)", index_name)
        except sqlite3.Error as exc:
            logger.warning("composite-keys: failed to drop %s — %s", index_name, exc)


def _migrate_chain_status_complete_rename(conn: sqlite3.Connection) -> None:
    """OI-830: Rename chain_status 'complete' -> 'receipt_and_commit' in provenance_registry.

    Idempotent: after the first run, no rows match the WHERE clause and the
    statement is a no-op. Only touches rows that actually have the old value.
    The provenance_registry table may not exist yet on fresh installs — skip
    silently when it's absent.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provenance_registry'"
    ).fetchone():
        return
    conn.execute(
        "UPDATE provenance_registry SET chain_status = 'receipt_and_commit' "
        "WHERE chain_status = 'complete'"
    )
    conn.commit()


def _rc_project_id_present(conn: sqlite3.Connection) -> bool:
    """Return True if at least one ADR-007 target table exists and has project_id."""
    for table, _ in _RC_COMPOSITE_KEYS:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "project_id" in cols:
            return True
    return False


def init_schema(state_dir: str | Path, schema_sql_path: Optional[Path] = None) -> None:
    """Initialize (or migrate) the runtime coordination database. Idempotent.

    Tracks applied migrations via PRAGMA user_version (primary) and
    runtime_schema_version table (secondary, preserved for backward compat).
    Base schema = user_version 1; each versioned SQL file increments by 1.

    Uses a single connection for the entire init+migration sequence to
    prevent TOCTOU races between version check and migration apply.
    """
    if schema_sql_path is None:
        here = Path(__file__).resolve()
        schema_sql_path = here.parent.parent.parent / "schemas" / "runtime_coordination.sql"

    if not schema_sql_path.exists():
        raise FileNotFoundError(f"Runtime coordination schema not found: {schema_sql_path}")

    schema_sql = schema_sql_path.read_text(encoding="utf-8")

    with get_connection(state_dir) as conn:
        # V1: base schema applied atomically (codex round-2 fix — script + version stamp in one SAVEPOINT)
        if _needs_initial_migration(conn):
            schema_migration.apply_script_if_below(conn, 1, schema_sql)

        # Versioned migration files: runtime_coordination_v2.sql, v3.sql, ...
        schemas_dir = schema_sql_path.parent
        v = 2
        while True:
            migration_path = schemas_dir / f"runtime_coordination_v{v}.sql"
            if not migration_path.exists():
                break
            migration_sql = migration_path.read_text(encoding="utf-8")
            schema_migration.apply_script_if_below(conn, v, migration_sql)
            v += 1

        # V11: ADR-007 composite UNIQUE indexes over project_id. Only schedule when
        # project_id is already present (migration 0010 runs after init_schema in the
        # normal vnx migrate flow). If project_id is absent here, run_runtime_coordination_migration
        # will apply V11 after adding the column.
        if _rc_project_id_present(conn):
            schema_migration.apply_if_below(conn, 11, _migrate_v11_composite_keys)

        # OI-830: rename chain_status value 'complete' -> 'receipt_and_commit'.
        # Idempotent — after the first run no rows match the WHERE clause.
        _migrate_chain_status_complete_rename(conn)


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
    pid_clause = " AND project_id = ?" if (_has_project_id_col(conn, "terminal_leases") and _project_filter_enabled()) else ""
    params = (terminal_id, _current_project_id()) if pid_clause else (terminal_id,)
    row = conn.execute(
        f"SELECT * FROM terminal_leases WHERE terminal_id = ?{pid_clause}", params
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
    if _has_project_id_col(conn, "coordination_events") and _project_filter_enabled():
        clauses.append("project_id = ?")
        params.append(_current_project_id())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM coordination_events {where} ORDER BY occurred_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def project_terminal_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Project current terminal_leases state into terminal_state.json format."""
    if _has_project_id_col(conn, "terminal_leases") and _project_filter_enabled():
        rows = conn.execute(
            "SELECT * FROM terminal_leases WHERE project_id = ?", (_current_project_id(),)
        ).fetchall()
    else:
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
