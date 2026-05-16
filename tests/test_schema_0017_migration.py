"""Tests for migration 0017 — multi-tenant lease isolation (schema v12).

Covers: apply from pre-migration state, idempotency, rollback on error,
composite UNIQUE enforcement, worker_states.project_id presence,
ADR-005 audit event emission, and composite FK correctness.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/lib importable
_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from migrations.apply_0017 import apply_migration

MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "migrations"
    / "0017_multi_tenant_lease_isolation.sql"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_pre_migration_db(db_path: Path) -> None:
    """Build a minimal DB that represents the pre-0017 state (v11).

    terminal_leases and dispatches have project_id (from 0010) but only
    single-column UNIQUE constraints. worker_states has no project_id.
    dispatch_attempts has project_id (from 0010) with single-column FK.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE runtime_schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                description TEXT NOT NULL
            );
            INSERT INTO runtime_schema_version VALUES (1, datetime('now'), 'initial');
            INSERT INTO runtime_schema_version VALUES (9, datetime('now'), 'worker_states');
            INSERT INTO runtime_schema_version VALUES (10, datetime('now'), 'project_id phase 0');
            INSERT INTO runtime_schema_version VALUES (11, datetime('now'), 'project_id phase 4');

            CREATE TABLE dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL UNIQUE,
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
                state           TEXT    NOT NULL DEFAULT 'queued',
                terminal_id     TEXT,
                track           TEXT,
                priority        TEXT    DEFAULT 'P2',
                pr_ref          TEXT,
                gate            TEXT,
                attempt_count   INTEGER NOT NULL DEFAULT 0,
                bundle_path     TEXT,
                created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                expires_after   TEXT,
                metadata_json   TEXT    DEFAULT '{}'
            );

            CREATE TABLE terminal_leases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id         TEXT    NOT NULL UNIQUE,
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
                state               TEXT    NOT NULL DEFAULT 'idle',
                dispatch_id         TEXT,
                generation          INTEGER NOT NULL DEFAULT 1,
                leased_at           TEXT,
                expires_at          TEXT,
                last_heartbeat_at   TEXT,
                released_at         TEXT,
                metadata_json       TEXT    DEFAULT '{}'
            );
            INSERT INTO terminal_leases (terminal_id, state, generation)
                VALUES ('T1', 'idle', 1), ('T2', 'idle', 1), ('T3', 'idle', 1);

            CREATE TABLE worker_states (
                terminal_id      TEXT    NOT NULL PRIMARY KEY,
                dispatch_id      TEXT    NOT NULL,
                state            TEXT    NOT NULL DEFAULT 'initializing',
                last_output_at   TEXT,
                state_entered_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                stall_count      INTEGER NOT NULL DEFAULT 0,
                blocked_reason   TEXT,
                metadata_json    TEXT,
                created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE dispatch_attempts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id      TEXT    NOT NULL UNIQUE,
                dispatch_id     TEXT    NOT NULL REFERENCES dispatches (dispatch_id),
                attempt_number  INTEGER NOT NULL DEFAULT 1,
                terminal_id     TEXT    NOT NULL,
                state           TEXT    NOT NULL DEFAULT 'pending',
                started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                ended_at        TEXT,
                failure_reason  TEXT,
                metadata_json   TEXT    DEFAULT '{}',
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev'
            );
        """)
    finally:
        conn.close()


def _has_composite_unique(db_path: Path, table: str, columns: frozenset) -> bool:
    """Return True if the table has a UNIQUE index over exactly the given columns."""
    conn = sqlite3.connect(str(db_path))
    try:
        indices = conn.execute(f"PRAGMA index_list({table})").fetchall()
        for idx in indices:
            # index_list row: (seq, name, unique, origin, partial)
            if not idx[2]:  # unique flag
                continue
            info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
            # index_info row: (seqno, cid, name)
            idx_cols = frozenset(row[2] for row in info)
            if idx_cols == columns:
                return True
    finally:
        conn.close()
    return False


def _max_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(version) FROM runtime_schema_version").fetchone()
        return int(row[0]) if (row and row[0] is not None) else 0
    finally:
        conn.close()


def _read_ndjson_events(events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_apply_migration_from_v9_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    result = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert result is True
    assert _max_version(db) == 12
    assert _has_composite_unique(db, "terminal_leases", frozenset({"terminal_id", "project_id"}))
    assert _has_composite_unique(db, "dispatches", frozenset({"dispatch_id", "project_id"}))


def test_apply_migration_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    first = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)
    second = apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    assert first is True
    assert second is False
    assert _max_version(db) == 12


def test_apply_migration_rollback_on_error(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    corrupt_sql = tmp_path / "corrupt.sql"
    _create_pre_migration_db(db)

    corrupt_sql.write_text("""
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
ALTER TABLE worker_states ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
THIS IS NOT VALID SQL AND WILL CAUSE AN ERROR;
COMMIT;
PRAGMA foreign_keys = ON;
""")

    with pytest.raises(sqlite3.OperationalError):
        apply_migration(db, corrupt_sql, vnx_data_dir=tmp_path)

    # worker_states must not have project_id (transaction rolled back)
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(worker_states)")}
    finally:
        conn.close()
    assert "project_id" not in cols

    # DB version unchanged
    assert _max_version(db) == 11


def test_terminal_leases_unique_constraint(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        # (T1, proj-a) and (T1, proj-b) in the same table are allowed
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
            " VALUES ('T1', 'proj-a', 'idle', 1)"
        )
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
            " VALUES ('T1', 'proj-b', 'idle', 1)"
        )
        conn.commit()

        # Duplicate (T1, proj-a) must raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_leases (terminal_id, project_id, state, generation)"
                " VALUES ('T1', 'proj-a', 'idle', 2)"
            )
            conn.commit()
    finally:
        conn.close()


def test_worker_states_has_project_id_after_migration(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(worker_states)")}
    finally:
        conn.close()
    assert "project_id" in cols


def test_migration_emits_started_completed_events(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)

    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    events_path = tmp_path / "events" / "schema_migrations.ndjson"
    events = _read_ndjson_events(events_path)
    assert len(events) == 2
    assert events[0]["event_type"] == "migration_started"
    assert events[1]["event_type"] == "migration_completed"
    assert events[0]["migration"] == "0017_multi_tenant_lease_isolation"
    assert events[1]["migration"] == "0017_multi_tenant_lease_isolation"


def test_migration_emits_failed_event_on_rollback(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    corrupt_sql = tmp_path / "corrupt.sql"
    _create_pre_migration_db(db)

    corrupt_sql.write_text("""
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
ALTER TABLE worker_states ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
THIS IS NOT VALID SQL AND WILL CAUSE AN ERROR;
COMMIT;
PRAGMA foreign_keys = ON;
""")

    with pytest.raises(sqlite3.OperationalError):
        apply_migration(db, corrupt_sql, vnx_data_dir=tmp_path)

    events_path = tmp_path / "events" / "schema_migrations.ndjson"
    events = _read_ndjson_events(events_path)
    event_types = [e["event_type"] for e in events]
    assert "migration_failed" in event_types
    failed = next(e for e in events if e["event_type"] == "migration_failed")
    assert "error" in failed


def test_composite_fk_referencing_dispatches(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _create_pre_migration_db(db)
    apply_migration(db, MIGRATION_SQL, vnx_data_dir=tmp_path)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state)"
            " VALUES ('d1', 'proj-a', 'queued')"
        )
        conn.commit()

        # terminal_leases: valid (dispatch_id, project_id) pair → success
        conn.execute(
            "INSERT INTO terminal_leases"
            " (terminal_id, project_id, state, dispatch_id, generation)"
            " VALUES ('T4', 'proj-a', 'leased', 'd1', 1)"
        )
        conn.commit()

        # terminal_leases: unknown dispatch_id → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_leases"
                " (terminal_id, project_id, state, dispatch_id, generation)"
                " VALUES ('T5', 'proj-a', 'leased', 'nonexistent', 1)"
            )
            conn.commit()

        # dispatch_attempts: valid (dispatch_id, project_id) pair → success
        conn.execute(
            "INSERT INTO dispatch_attempts"
            " (attempt_id, dispatch_id, project_id, terminal_id)"
            " VALUES ('a1', 'd1', 'proj-a', 'T1')"
        )
        conn.commit()

        # dispatch_attempts: unknown dispatch → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_attempts"
                " (attempt_id, dispatch_id, project_id, terminal_id)"
                " VALUES ('a2', 'nonexistent', 'proj-a', 'T1')"
            )
            conn.commit()
    finally:
        conn.close()
