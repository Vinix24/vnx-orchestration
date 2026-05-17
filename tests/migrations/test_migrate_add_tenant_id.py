"""End-to-end tests for migrate_add_tenant_id.

These exercise the real runner against real SQLite files (per the codex
defense checklist: tests must run actual code, not reimplement it).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "migrations"))

import migrate_add_tenant_id_runner as runner  # noqa: E402


SQL_PATH = REPO_ROOT / "scripts" / "migrations" / "migrate_add_tenant_id.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_events(db_path: str, n_rows: int = 250, n_users: int = 10) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE events ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id INTEGER,"
            "  payload TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE tenant_mapping ("
            "  user_id INTEGER PRIMARY KEY,"
            "  tenant_id INTEGER NOT NULL"
            ")"
        )
        # First half of users mapped to tenant 100, rest unmapped (default-tenant).
        mapped = [(uid, 100) for uid in range(n_users // 2)]
        conn.executemany(
            "INSERT INTO tenant_mapping(user_id, tenant_id) VALUES (?, ?)",
            mapped,
        )
        events = [(i % n_users if i % 7 else None, f"p{i}") for i in range(n_rows)]
        conn.executemany(
            "INSERT INTO events(user_id, payload) VALUES (?, ?)",
            events,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = tmp_path / "events.db"
    _seed_events(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# parse_sql_sections
# ---------------------------------------------------------------------------

def test_parse_sql_sections_finds_all_sections():
    sections = runner.parse_sql_sections(SQL_PATH)
    assert {"prepare", "forward", "rollback"} <= set(sections.keys())
    assert "migration_log" in sections["prepare"]
    assert "DROP TRIGGER" in sections["rollback"]


def test_parse_sql_sections_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        runner.parse_sql_sections(tmp_path / "does_not_exist.sql")


# ---------------------------------------------------------------------------
# Forward migration
# ---------------------------------------------------------------------------

def test_forward_backfills_all_rows(db: str):
    opts = runner.ForwardOptions(
        db_path=db,
        sql_path=SQL_PATH,
        default_tenant_id=999,
        batch_size=37,  # deliberate non-multiple to hit edge case
    )
    rows = runner.run_forward(opts)
    assert rows == 250

    conn = sqlite3.connect(db)
    try:
        nulls = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        mapped = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = 100"
        ).fetchone()[0]
        assert mapped > 0
        defaulted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = 999"
        ).fetchone()[0]
        assert defaulted > 0
        assert mapped + defaulted == 250
    finally:
        conn.close()


def test_forward_is_idempotent(db: str):
    opts = runner.ForwardOptions(
        db_path=db,
        sql_path=SQL_PATH,
        default_tenant_id=999,
        batch_size=64,
    )
    first = runner.run_forward(opts)
    assert first == 250
    second = runner.run_forward(opts)
    assert second == 0  # nothing left to do

    conn = sqlite3.connect(db)
    try:
        status = conn.execute(
            "SELECT status FROM migration_state WHERE migration = ?",
            (runner.MIGRATION_NAME,),
        ).fetchone()[0]
        assert status == "complete"
    finally:
        conn.close()


def test_forward_resumes_from_last_rowid(db: str):
    # Simulate a previous partial run by writing a high last_rowid.
    conn = sqlite3.connect(db)
    try:
        conn.executescript(runner.parse_sql_sections(SQL_PATH)["prepare"])
        conn.execute("ALTER TABLE events ADD COLUMN tenant_id INTEGER")
        # Pre-fill rowids 1..100 manually.
        conn.executemany(
            "UPDATE events SET tenant_id = 7 WHERE rowid = ?",
            [(i,) for i in range(1, 101)],
        )
        conn.execute(
            "UPDATE migration_state SET last_rowid = 100, status='running' "
            "WHERE migration = ?",
            (runner.MIGRATION_NAME,),
        )
        conn.commit()
    finally:
        conn.close()

    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=999,
            batch_size=50,
        )
    )

    conn = sqlite3.connect(db)
    try:
        # The pre-filled rows must keep tenant 7 — runner must not overwrite.
        preserved = conn.execute(
            "SELECT COUNT(*) FROM events WHERE rowid <= 100 AND tenant_id = 7"
        ).fetchone()[0]
        assert preserved == 100
        nulls = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
        ).fetchone()[0]
        assert nulls == 0
    finally:
        conn.close()


def test_not_null_triggers_block_null_inserts(db: str):
    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=999,
            batch_size=128,
        )
    )
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="tenant_id must not be NULL"):
            conn.execute(
                "INSERT INTO events(user_id, payload, tenant_id) VALUES (?, ?, ?)",
                (1, "x", None),
            )
        with pytest.raises(sqlite3.IntegrityError, match="tenant_id must not be NULL"):
            conn.execute(
                "UPDATE events SET tenant_id = NULL WHERE rowid = 1"
            )
        # Non-null write still works.
        conn.execute(
            "INSERT INTO events(user_id, payload, tenant_id) VALUES (?, ?, ?)",
            (1, "x", 42),
        )
        conn.commit()
    finally:
        conn.close()


def test_audit_log_records_lifecycle(db: str):
    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=999,
            batch_size=80,
        )
    )
    conn = sqlite3.connect(db)
    try:
        events = [
            r[0]
            for r in conn.execute(
                "SELECT event FROM migration_log WHERE migration = ? ORDER BY id",
                (runner.MIGRATION_NAME,),
            )
        ]
        assert events[0] == "start"
        assert "batch" in events
        assert events[-1] == "finish"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def test_rollback_undoes_migration(db: str):
    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=999,
            batch_size=128,
        )
    )
    runner.run_rollback(db, SQL_PATH)

    conn = sqlite3.connect(db)
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(events)")]
        # On SQLite < 3.35 the column is dropped via rebuild; either way it must be gone.
        assert "tenant_id" not in cols
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        assert all("tenant_id" not in t[0] for t in triggers)
        status = conn.execute(
            "SELECT status FROM migration_state WHERE migration = ?",
            (runner.MIGRATION_NAME,),
        ).fetchone()[0]
        assert status == "rolled_back"
        rollback_events = conn.execute(
            "SELECT COUNT(*) FROM migration_log "
            "WHERE migration = ? AND event = 'rollback_finish'",
            (runner.MIGRATION_NAME,),
        ).fetchone()[0]
        assert rollback_events == 1
    finally:
        conn.close()


def test_rollback_is_idempotent(db: str):
    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=999,
            batch_size=128,
        )
    )
    runner.run_rollback(db, SQL_PATH)
    runner.run_rollback(db, SQL_PATH)  # must not raise


# ---------------------------------------------------------------------------
# CLI / negative paths
# ---------------------------------------------------------------------------

def test_cli_forward_requires_default_tenant_id(db: str, capsys):
    rc = runner.main(["--db", db])
    assert rc == 2


def test_cli_rejects_zero_batch_size(db: str):
    rc = runner.main(
        ["--db", db, "--default-tenant-id", "1", "--batch-size", "0"]
    )
    assert rc == 2


def test_cli_forward_then_rollback_via_main(db: str):
    rc = runner.main(
        ["--db", db, "--default-tenant-id", "1", "--batch-size", "100"]
    )
    assert rc == 0
    rc = runner.main(["--db", db, "--direction", "down"])
    assert rc == 0


def test_all_default_ignores_tenant_mapping(db: str):
    runner.run_forward(
        runner.ForwardOptions(
            db_path=db,
            sql_path=SQL_PATH,
            default_tenant_id=42,
            batch_size=128,
            all_default=True,
        )
    )
    conn = sqlite3.connect(db)
    try:
        non_default = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id != 42"
        ).fetchone()[0]
        assert non_default == 0
    finally:
        conn.close()
