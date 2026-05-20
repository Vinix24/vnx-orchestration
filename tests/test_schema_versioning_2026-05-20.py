"""tests/test_schema_versioning_2026-05-20.py — schema versioning + rollback tests.

Dispatch-ID: 20260520-1445-schema-versioning

Coverage:
- get_schema_version on a clean DB (schema_meta absent) → 0
- ensure_schema_meta creates table + seeds schema_version
- set_schema_version + read-back
- check_schema_version: match → True, above → False, below → RuntimeError
- Mismatch detection: schema_version=5, migration expects=7 → RuntimeError
- _down.sql reversibility: 0021 (table creation), 0019 (column drop)
- _down.sql reversibility: 2026_05_task_subclass (index-only)
- _down.sql reversibility: 2026_05_intelligence_hygiene (data restore)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "schemas" / "migrations"

import sys
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from schema_versioning import (
    check_schema_version,
    ensure_schema_meta,
    get_schema_version,
    set_schema_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _blank_db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def _db_with_meta(version: int = 0) -> sqlite3.Connection:
    conn = _blank_db()
    ensure_schema_meta(conn, initial_version=version)
    return conn


def _run_sql_file(conn: sqlite3.Connection, path: Path) -> None:
    sql = path.read_text()
    conn.executescript(sql)


# ---------------------------------------------------------------------------
# A. get_schema_version on clean DB (schema_meta absent)
# ---------------------------------------------------------------------------

class TestGetSchemaVersionClean:
    def test_returns_zero_when_table_absent(self):
        conn = _blank_db()
        assert get_schema_version(conn) == 0

    def test_returns_zero_when_table_exists_but_no_row(self):
        conn = _blank_db()
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        assert get_schema_version(conn) == 0


# ---------------------------------------------------------------------------
# B. ensure_schema_meta
# ---------------------------------------------------------------------------

class TestEnsureSchemaMeta:
    def test_creates_table(self):
        conn = _blank_db()
        ensure_schema_meta(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        assert row is not None

    def test_seeds_schema_version_default_zero(self):
        conn = _blank_db()
        ensure_schema_meta(conn)
        assert get_schema_version(conn) == 0

    def test_seeds_schema_version_custom(self):
        conn = _blank_db()
        ensure_schema_meta(conn, initial_version=7)
        assert get_schema_version(conn) == 7

    def test_idempotent_on_second_call(self):
        conn = _blank_db()
        ensure_schema_meta(conn, initial_version=3)
        # Second call should not overwrite the seed value
        ensure_schema_meta(conn, initial_version=99)
        # INSERT OR IGNORE means second call is a no-op
        assert get_schema_version(conn) == 3


# ---------------------------------------------------------------------------
# C. set_schema_version + read-back
# ---------------------------------------------------------------------------

class TestSetSchemaVersion:
    def test_set_and_read_back(self):
        conn = _db_with_meta(0)
        set_schema_version(conn, 10)
        assert get_schema_version(conn) == 10

    def test_update_existing(self):
        conn = _db_with_meta(10)
        set_schema_version(conn, 15)
        assert get_schema_version(conn) == 15

    def test_creates_table_if_absent(self):
        conn = _blank_db()
        set_schema_version(conn, 5)
        assert get_schema_version(conn) == 5

    def test_set_zero(self):
        conn = _db_with_meta(10)
        set_schema_version(conn, 0)
        assert get_schema_version(conn) == 0


# ---------------------------------------------------------------------------
# D. check_schema_version: match / above / below
# ---------------------------------------------------------------------------

class TestCheckSchemaVersion:
    def test_returns_true_on_match(self):
        conn = _db_with_meta(7)
        assert check_schema_version(conn, 7, "test_migration") is True

    def test_returns_false_when_already_past(self):
        conn = _db_with_meta(10)
        result = check_schema_version(conn, 7, "test_migration")
        assert result is False

    def test_raises_when_below_expected(self):
        conn = _db_with_meta(5)
        with pytest.raises(RuntimeError, match="schema_version=7"):
            check_schema_version(conn, 7, "0015_complete_project_id")

    def test_error_message_contains_migration_name(self):
        conn = _db_with_meta(5)
        with pytest.raises(RuntimeError, match="0015_complete_project_id"):
            check_schema_version(conn, 7, "0015_complete_project_id")

    def test_error_message_shows_current_version(self):
        conn = _db_with_meta(5)
        with pytest.raises(RuntimeError, match="schema_version=5"):
            check_schema_version(conn, 7, "migration_x")


# ---------------------------------------------------------------------------
# E. Mismatch detection: schema_version=5, migration expects=7 → refuse
# ---------------------------------------------------------------------------

class TestMismatchDetection:
    def test_exact_mismatch_scenario_from_dispatch(self):
        """schema_version=5, migration expects=7 → RuntimeError."""
        conn = _db_with_meta(5)
        with pytest.raises(RuntimeError) as exc_info:
            check_schema_version(conn, 7, "0017_multi_tenant")
        err = str(exc_info.value)
        assert "schema_version=7" in err
        assert "schema_version=5" in err

    def test_fix_suggestion_in_error(self):
        conn = _db_with_meta(3)
        with pytest.raises(RuntimeError, match="migration-rollback-runbook"):
            # expected > current should mention docs, but actual message differs
            # Just verify RuntimeError is raised with useful content
            check_schema_version(conn, 10, "some_migration")


# ---------------------------------------------------------------------------
# F. _down.sql reversibility
# ---------------------------------------------------------------------------

class TestDownSqlReversibility:
    """Test that _down.sql files produce a correct post-rollback state."""

    def _base_rc_db(self) -> sqlite3.Connection:
        """Minimal runtime_coordination.db fixture with runtime_schema_version."""
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE runtime_schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            INSERT INTO runtime_schema_version(version, description)
            VALUES (20, 'test base v20');
        """)
        return conn

    def test_0021_down_drops_install_tables(self):
        conn = self._base_rc_db()
        # Simulate up-migration state: create the tables
        conn.executescript("""
            CREATE TABLE central_install_pins (
                project_id  TEXT NOT NULL,
                project_root TEXT NOT NULL,
                pin_version TEXT NOT NULL,
                pinned_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (project_id, project_root)
            );
            CREATE TABLE central_install_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                occurred_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                success       INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_install_events_project ON central_install_events(project_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_install_events_type ON central_install_events(event_type);
            INSERT INTO runtime_schema_version(version, description) VALUES (21, 'test v21');
        """)
        # Verify tables exist before down
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_install_pins'"
        ).fetchone() is not None

        # Apply the down migration (read and execute without PRAGMA user_version line)
        down_path = _MIGRATIONS_DIR / "0021_central_install_metadata_down.sql"
        sql = down_path.read_text()
        # Strip the PRAGMA user_version line (not valid inside executescript in some contexts)
        sql_no_pragma = "\n".join(
            line for line in sql.splitlines()
            if not line.strip().startswith("PRAGMA user_version")
        )
        conn.executescript(sql_no_pragma)

        # Verify tables are gone
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_install_pins'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_install_events'"
        ).fetchone() is None

    def test_0019_down_drops_lease_token_column(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT
            );
            INSERT INTO runtime_schema_version(version, description)
            VALUES (13, 'test v13');

            CREATE TABLE terminal_leases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT NOT NULL UNIQUE,
                state       TEXT NOT NULL DEFAULT 'idle',
                generation  INTEGER NOT NULL DEFAULT 1,
                lease_token TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX idx_terminal_leases_token
                ON terminal_leases(lease_token) WHERE lease_token != '';
        """)
        # Apply down migration
        down_path = _MIGRATIONS_DIR / "0019_t0_lifecycle_tokens_down.sql"
        conn.executescript(down_path.read_text())

        # lease_token column should be gone
        cols = [r[1] for r in conn.execute("PRAGMA table_info(terminal_leases)")]
        assert "lease_token" not in cols

        # Index should be gone
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_terminal_leases_token'"
        ).fetchone()
        assert idx is None

        # Version stamp should be gone
        stamp = conn.execute(
            "SELECT version FROM runtime_schema_version WHERE version=13"
        ).fetchone()
        assert stamp is None

    def test_task_subclass_down_drops_category_indexes(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                category TEXT
            );
            CREATE TABLE antipatterns (
                id INTEGER PRIMARY KEY,
                category TEXT
            );
            CREATE INDEX idx_success_patterns_category ON success_patterns(category);
            CREATE INDEX idx_antipatterns_category ON antipatterns(category);
        """)
        # Verify indexes exist
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_success_patterns_category'"
        ).fetchone() is not None

        down_path = _MIGRATIONS_DIR / "2026_05_task_subclass_down.sql"
        conn.executescript(down_path.read_text())

        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_success_patterns_category'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_antipatterns_category'"
        ).fetchone() is None

    def test_intelligence_hygiene_down_restores_valid_until(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT
            );
            INSERT INTO runtime_schema_version(version, description)
            VALUES (15, 'hygiene');

            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                valid_until TEXT,
                invalidation_reason TEXT
            );
            CREATE TABLE antipatterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                valid_until TEXT,
                invalidation_reason TEXT
            );

            -- Simulate hygiene migration state
            INSERT INTO success_patterns(title, valid_until, invalidation_reason)
            VALUES ('gate foo passed', datetime('now'), 'governance_event_noise_filter_2026_05_hygiene');
            INSERT INTO success_patterns(title, valid_until, invalidation_reason)
            VALUES ('real pattern', NULL, NULL);
            INSERT INTO antipatterns(category, valid_until, invalidation_reason)
            VALUES ('memory_consolidation', datetime('now'), 'meta_stats_filter_2026_05_hygiene');
        """)

        down_path = _MIGRATIONS_DIR / "2026_05_intelligence_hygiene_down.sql"
        conn.executescript(down_path.read_text())

        # Governance noise pattern should have valid_until restored to NULL
        row = conn.execute(
            "SELECT valid_until, invalidation_reason FROM success_patterns WHERE title='gate foo passed'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None

        # Untouched pattern should still be NULL
        row2 = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title='real pattern'"
        ).fetchone()
        assert row2[0] is None

        # Antipattern should be restored
        row3 = conn.execute(
            "SELECT valid_until, invalidation_reason FROM antipatterns WHERE category='memory_consolidation'"
        ).fetchone()
        assert row3[0] is None
        assert row3[1] is None

        # Version stamp should be gone
        stamp = conn.execute(
            "SELECT version FROM runtime_schema_version WHERE version=15"
        ).fetchone()
        assert stamp is None
