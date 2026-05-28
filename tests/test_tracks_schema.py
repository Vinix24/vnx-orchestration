"""tests/test_tracks_schema.py — migration 0022 schema validation.

Verifies:
- Migration runs cleanly on a fresh DB with existing dispatches
- Four new tables created with correct structure
- CHECK constraints reject invalid values
- UNIQUE next_up index enforced
- Dispatches table extended with operator_approved_at
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration


def _base_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal coordination DB with the base dispatches table."""
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
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
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            event_type  TEXT,
            entity_type TEXT,
            entity_id   TEXT,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT,
            reason      TEXT,
            metadata_json TEXT,
            occurred_at TEXT,
            project_id  TEXT
        )
    """)
    conn.commit()
    return conn


def _apply_migration_0022(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


# ---------------------------------------------------------------------------
# Migration correctness
# ---------------------------------------------------------------------------

class TestMigration0022:
    def test_migration_applies_on_fresh_db(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        assert schema_migration.get_user_version(conn) == 22

    def test_all_four_tables_created(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        tables = _table_names(conn)
        assert "tracks" in tables
        assert "track_phase_history" in tables
        assert "track_dependencies" in tables
        assert "track_open_items" in tables

    def test_dispatches_extended_with_operator_approved_at(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        cols = _column_names(conn, "dispatches")
        assert "operator_approved_at" in cols

    def test_existing_dispatch_data_preserved(self, tmp_path):
        conn = _base_db(tmp_path)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state) VALUES ('disp-001', 'queued')"
        )
        conn.commit()
        _apply_migration_0022(conn)
        row = conn.execute(
            "SELECT dispatch_id, state FROM dispatches WHERE dispatch_id = 'disp-001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "disp-001"
        assert row[1] == "queued"

    def test_migration_is_idempotent(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        _apply_migration_0022(conn)  # second call must be a no-op
        assert schema_migration.get_user_version(conn) == 22

    def test_required_indexes_created(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        indexes = _index_names(conn)
        assert "idx_tracks_phase_nextup" in indexes
        assert "ux_tracks_next_up" in indexes
        assert "idx_dispatches_ready" in indexes
        assert "idx_track_deps_from" in indexes
        assert "idx_track_phase_history" in indexes


# ---------------------------------------------------------------------------
# Tracks table constraints
# ---------------------------------------------------------------------------

class TestTracksConstraints:
    def _migrated_conn(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        return conn

    def test_valid_phase_accepted(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        for phase in ("queued", "active", "parked", "done"):
            conn.execute(
                "INSERT INTO tracks (track_id, title, goal_state, phase) VALUES (?, 'T', 'G', ?)",
                (f"track-{phase}", phase),
            )
        conn.commit()

    def test_invalid_phase_rejected(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks (track_id, title, goal_state, phase) "
                "VALUES ('track-bad', 'T', 'G', 'invalid_phase')"
            )
            conn.commit()

    def test_unique_next_up_enforced(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
            "VALUES ('track-01', 'T1', 'G1', 'queued', 1)"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
                "VALUES ('track-02', 'T2', 'G2', 'queued', 1)"
            )
            conn.commit()

    def test_unique_next_up_allows_non_queued_duplicate(self, tmp_path):
        """next_up=1 on an active track does not conflict with queued next_up=1."""
        conn = self._migrated_conn(tmp_path)
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
            "VALUES ('track-01', 'T1', 'G1', 'queued', 1)"
        )
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
            "VALUES ('track-02', 'T2', 'G2', 'active', 1)"
        )
        conn.commit()

    def test_unique_next_up_allows_zero(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
            "VALUES ('track-01', 'T1', 'G1', 'queued', 0)"
        )
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase, next_up) "
            "VALUES ('track-02', 'T2', 'G2', 'queued', 0)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Dispatches state constraint
# ---------------------------------------------------------------------------

class TestDispatchesStateConstraint:
    def _migrated_conn(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        return conn

    def test_valid_future_states_accepted(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        for state in ("proposed", "ready", "active", "completed", "failed"):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES (?, ?)",
                (f"disp-{state}", state),
            )
        conn.commit()

    def test_legacy_states_accepted(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        for state in ("queued", "claimed", "running", "timed_out", "dead_letter"):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES (?, ?)",
                (f"disp-leg-{state}", state),
            )
        conn.commit()

    def test_invalid_dispatch_state_rejected(self, tmp_path):
        conn = self._migrated_conn(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES ('disp-bad', 'bogus_state')"
            )
            conn.commit()


# ---------------------------------------------------------------------------
# track_dependencies constraints
# ---------------------------------------------------------------------------

class TestTrackDependenciesConstraints:
    def _migrated_conn_with_tracks(self, tmp_path):
        conn = _base_db(tmp_path)
        _apply_migration_0022(conn)
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase) "
            "VALUES ('track-01', 'T1', 'G1', 'active')"
        )
        conn.execute(
            "INSERT INTO tracks (track_id, title, goal_state, phase) "
            "VALUES ('track-02', 'T2', 'G2', 'queued')"
        )
        conn.commit()
        return conn

    def test_valid_dependency_inserted(self, tmp_path):
        conn = self._migrated_conn_with_tracks(tmp_path)
        conn.execute(
            "INSERT INTO track_dependencies "
            "(from_track_id, to_track_id, kind, derivation_source) "
            "VALUES ('track-02', 'track-01', 'hard', 'manual')"
        )
        conn.commit()

    def test_invalid_kind_rejected(self, tmp_path):
        conn = self._migrated_conn_with_tracks(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_dependencies "
                "(from_track_id, to_track_id, kind, derivation_source) "
                "VALUES ('track-02', 'track-01', 'invalid_kind', 'manual')"
            )
            conn.commit()

    def test_invalid_derivation_source_rejected(self, tmp_path):
        conn = self._migrated_conn_with_tracks(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO track_dependencies "
                "(from_track_id, to_track_id, kind, derivation_source) "
                "VALUES ('track-02', 'track-01', 'hard', 'invalid_source')"
            )
            conn.commit()


# ---------------------------------------------------------------------------
# v21 project_id + composite UNIQUE survival across migration 0022
# ---------------------------------------------------------------------------

class TestV21ProjectIdPreservation:
    """Migration 0022 on a v21 DB (with project_id + composite UNIQUE) must
    preserve project_id and enforce the composite UNIQUE post-migration."""

    def _v21_db(self, tmp_path: Path) -> sqlite3.Connection:
        """Build a v21-style DB with project_id and composite UNIQUE."""
        db_path = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL,
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
                metadata_json   TEXT    DEFAULT '{}',
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coordination_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT,
                event_type  TEXT,
                entity_type TEXT,
                entity_id   TEXT,
                from_state  TEXT,
                to_state    TEXT,
                actor       TEXT,
                reason      TEXT,
                metadata_json TEXT,
                occurred_at TEXT,
                project_id  TEXT
            )
        """)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES ('d-x', 'vnx-dev', 'queued')"
        )
        conn.commit()
        return conn

    def test_migration_does_not_crash_on_v21(self, tmp_path):
        conn = self._v21_db(tmp_path)
        _apply_migration_0022(conn)

    def test_project_id_column_present_after_migration(self, tmp_path):
        conn = self._v21_db(tmp_path)
        _apply_migration_0022(conn)
        cols = _column_names(conn, "dispatches")
        assert "project_id" in cols

    def test_existing_row_project_id_preserved(self, tmp_path):
        conn = self._v21_db(tmp_path)
        _apply_migration_0022(conn)
        row = conn.execute(
            "SELECT project_id FROM dispatches WHERE dispatch_id = 'd-x'"
        ).fetchone()
        assert row is not None
        assert row[0] == "vnx-dev"

    def test_composite_unique_enforced_after_migration(self, tmp_path):
        conn = self._v21_db(tmp_path)
        _apply_migration_0022(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state) "
                "VALUES ('d-x', 'vnx-dev', 'queued')"
            )
            conn.commit()


# ---------------------------------------------------------------------------
# sqlite_sequence preservation across 0022 rebuild
# ---------------------------------------------------------------------------

class TestSqliteSequencePreservation:
    """AUTOINCREMENT sequence must survive the RENAME→CREATE→INSERT→DROP rebuild
    in 0022 so IDs stay monotonically increasing."""

    def _base_db_with_rows(self, tmp_path: Path, n: int) -> sqlite3.Connection:
        """Build a pre-0022 DB with n dispatch rows."""
        db_path = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL,
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
                metadata_json   TEXT    DEFAULT '{}',
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coordination_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT,
                event_type  TEXT,
                entity_type TEXT,
                entity_id   TEXT,
                from_state  TEXT,
                to_state    TEXT,
                actor       TEXT,
                reason       TEXT,
                metadata_json TEXT,
                occurred_at TEXT,
                project_id  TEXT
            )
        """)
        for i in range(n):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES (?, 'queued')",
                (f"disp-{i:03d}",),
            )
        conn.commit()
        return conn

    def test_sequence_preserved_after_0022(self, tmp_path):
        """After 0022 rebuild, the next auto-insert gets id > N (not reset to 1)."""
        n = 5
        conn = self._base_db_with_rows(tmp_path, n)
        sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, 22, sql)
        conn.commit()

        # Delete all rows to force reliance on sqlite_sequence for next id
        conn.execute("DELETE FROM dispatches")
        conn.commit()

        # Insert a new row and check the id is > n
        conn.execute("INSERT INTO dispatches (dispatch_id, state) VALUES ('new-post-0022', 'queued')")
        conn.commit()
        row = conn.execute("SELECT id FROM dispatches WHERE dispatch_id = 'new-post-0022'").fetchone()
        assert row is not None
        assert row[0] > n, (
            f"After 0022 rebuild, expected id > {n} but got {row[0]}. "
            "sqlite_sequence was not preserved."
        )
        conn.close()

