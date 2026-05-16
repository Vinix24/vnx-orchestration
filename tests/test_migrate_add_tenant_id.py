"""
tests/test_migrate_add_tenant_id.py

Functional tests for scripts/migrate_add_tenant_id_runner.py.
All tests run against an in-process SQLite :memory: or temp-file database
— no external dependencies, no mocks.
"""

import importlib.util
import sqlite3
import sys
import tempfile
import os
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Import runner without executing __main__
# ---------------------------------------------------------------------------

RUNNER_PATH = (
    Path(__file__).parent.parent / "scripts" / "migrate_add_tenant_id_runner.py"
)

spec = importlib.util.spec_from_file_location("runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_db(n_rows: int = 100, include_tenant_col: bool = False) -> str:
    """Create a temp SQLite file with an events table and n_rows rows."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    if include_tenant_col:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT, tenant_id TEXT)"
        )
    else:
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT)"
        )
    conn.executemany(
        "INSERT INTO events (payload) VALUES (?)",
        [(f"row_{i}",) for i in range(n_rows)],
    )
    conn.commit()
    conn.close()
    return f.name


@pytest.fixture
def db_path(tmp_path):
    f = tmp_path / "events.db"
    conn = sqlite3.connect(str(f))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.executemany(
        "INSERT INTO events (payload) VALUES (?)",
        [(f"row_{i}",) for i in range(150)],
    )
    conn.commit()
    conn.close()
    return str(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def open_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def audit_events(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT event, detail, row_count FROM migration_log ORDER BY id"
    ).fetchall()


def col_info(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("PRAGMA table_info(events)").fetchall()
    return {row[1]: row for row in rows}


# ---------------------------------------------------------------------------
# Tests — phase 1 (ADD COLUMN)
# ---------------------------------------------------------------------------

class TestPhase1AddColumn:
    def test_adds_tenant_id_when_missing(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        assert runner.column_exists(conn, "events", "tenant_id")
        conn.close()

    def test_idempotent_when_column_already_exists(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        # Second call must not raise
        runner.phase1_add_column(conn)
        assert runner.column_exists(conn, "events", "tenant_id")
        conn.close()

    def test_column_is_nullable_after_phase1(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        info = col_info(conn)
        # notnull flag == 0 means nullable
        assert info["tenant_id"][3] == 0
        conn.close()


# ---------------------------------------------------------------------------
# Tests — phase 2 (batched backfill)
# ---------------------------------------------------------------------------

class TestPhase2Backfill:
    def test_backfills_all_rows(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        total = runner.phase2_backfill(conn, "tenant_a", batch_size=50)
        assert total == 150
        null_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
        ).fetchone()[0]
        assert null_count == 0
        conn.close()

    def test_all_rows_get_correct_tenant(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        runner.phase2_backfill(conn, "acme", batch_size=50)
        distinct = conn.execute(
            "SELECT DISTINCT tenant_id FROM events"
        ).fetchall()
        assert distinct == [("acme",)]
        conn.close()

    def test_resume_from_last_committed_rowid(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        # Manually backfill first 75 rows
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE events SET tenant_id = 'acme' WHERE rowid <= 75"
        )
        conn.execute("COMMIT")
        # Runner should pick up from rowid 76
        total = runner.phase2_backfill(conn, "acme", batch_size=50)
        assert total == 75  # only the remaining 75
        null_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
        ).fetchone()[0]
        assert null_count == 0
        conn.close()

    def test_idempotent_when_already_backfilled(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        runner.phase2_backfill(conn, "acme", batch_size=50)
        # Second run should migrate 0 rows
        total2 = runner.phase2_backfill(conn, "acme", batch_size=50)
        assert total2 == 0
        conn.close()

    def test_empty_table_returns_zero(self, tmp_path):
        f = tmp_path / "empty.db"
        conn = sqlite3.connect(str(f), isolation_level=None)
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT)")
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        total = runner.phase2_backfill(conn, "acme")
        assert total == 0
        conn.close()


# ---------------------------------------------------------------------------
# Tests — phase 3 (NOT NULL enforcement)
# ---------------------------------------------------------------------------

class TestPhase3NotNull:
    def _run_phases_1_and_2(self, db_path: str, tenant: str = "acme"):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        runner.phase2_backfill(conn, tenant, batch_size=50)
        return conn

    def test_enforces_not_null(self, db_path):
        conn = self._run_phases_1_and_2(db_path)
        runner.phase3_enforce_not_null(conn)
        assert runner.column_is_not_null(conn, "events", "tenant_id")
        conn.close()

    def test_data_preserved_after_reconstruction(self, db_path):
        conn = self._run_phases_1_and_2(db_path)
        runner.phase3_enforce_not_null(conn)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 150
        conn.close()

    def test_idempotent_when_not_null_already_enforced(self, db_path):
        conn = self._run_phases_1_and_2(db_path)
        runner.phase3_enforce_not_null(conn)
        # Second call must not raise or corrupt data
        runner.phase3_enforce_not_null(conn)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 150
        conn.close()

    def test_aborts_when_nulls_remain(self, db_path):
        conn = open_conn(db_path)
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        # Do NOT backfill — some rows still NULL
        with pytest.raises(RuntimeError, match="tenant_id IS NULL"):
            runner.phase3_enforce_not_null(conn)
        conn.close()

    def test_indexes_recreated_after_reconstruction(self, tmp_path):
        f = tmp_path / "idx.db"
        conn = sqlite3.connect(str(f), isolation_level=None)
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("CREATE INDEX idx_events_payload ON events (payload)")
        conn.executemany(
            "INSERT INTO events (payload) VALUES (?)",
            [(f"v{i}",) for i in range(20)],
        )
        runner.setup_audit_table(conn)
        runner.phase1_add_column(conn)
        runner.phase2_backfill(conn, "t1", batch_size=10)
        runner.phase3_enforce_not_null(conn)
        idxs = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
        ).fetchall()
        idx_names = [r[0] for r in idxs]
        assert "idx_events_payload" in idx_names
        conn.close()


# ---------------------------------------------------------------------------
# Tests — full migration run_migration()
# ---------------------------------------------------------------------------

class TestRunMigration:
    def test_full_run_end_to_end(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        conn = open_conn(db_path)
        assert runner.column_is_not_null(conn, "events", "tenant_id")
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 150
        conn.close()

    def test_audit_log_has_start_and_finish(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        conn = open_conn(db_path)
        events = [row[0] for row in audit_events(conn)]
        assert "start" in events
        assert "backfill_complete" in events
        assert "not_null_enforced" in events
        assert "finish" in events
        conn.close()

    def test_full_run_idempotent(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        # Second run must complete without error and data must be intact
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        conn = open_conn(db_path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 150
        assert runner.column_is_not_null(conn, "events", "tenant_id")
        conn.close()


# ---------------------------------------------------------------------------
# Tests — rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_rollback_removes_column(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        runner.run_rollback(db_path)
        conn = open_conn(db_path)
        assert not runner.column_exists(conn, "events", "tenant_id")
        conn.close()

    def test_rollback_preserves_row_count(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        runner.run_rollback(db_path)
        conn = open_conn(db_path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 150
        conn.close()

    def test_rollback_idempotent_when_column_missing(self, db_path):
        runner.run_rollback(db_path)  # column never added — must not raise

    def test_rollback_logs_event(self, db_path):
        runner.run_migration(db_path, default_tenant="corp", batch_size=50)
        runner.run_rollback(db_path)
        conn = open_conn(db_path)
        events = [row[0] for row in audit_events(conn)]
        assert "rollback" in events
        conn.close()


# ---------------------------------------------------------------------------
# Tests — helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_build_not_null_ddl_adds_not_null(self):
        original = "CREATE TABLE events (id INTEGER PRIMARY KEY, tenant_id TEXT, val TEXT)"
        modified = runner.build_not_null_ddl(original, "tenant_id", "events_new")
        assert "tenant_id TEXT NOT NULL" in modified

    def test_build_not_null_ddl_renames_table(self):
        original = "CREATE TABLE events (id INTEGER PRIMARY KEY, tenant_id TEXT)"
        modified = runner.build_not_null_ddl(original, "tenant_id", "events_new")
        assert "CREATE TABLE events_new" in modified
        assert "CREATE TABLE events " not in modified

    def test_build_not_null_ddl_no_duplicate_not_null(self):
        original = "CREATE TABLE events (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL)"
        modified = runner.build_not_null_ddl(original, "tenant_id", "events_new")
        assert modified.count("NOT NULL") == 1
