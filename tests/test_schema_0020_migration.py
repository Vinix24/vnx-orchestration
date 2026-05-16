"""tests/test_schema_0020_migration.py — Wave 6 PR-6.2 schema migration tests.

Tests for schemas/migrations/0020_elastic_worker_pool.sql and its down-migration.
Uses in-memory SQLite; no filesystem dependencies except loading the SQL files.

Coverage:
- Three tables created with correct columns
- Indexes created
- Bootstrap rows inserted for vnx-dev/default
- Idempotency (re-run on v14 DB: no error, no change)
- apply_migration() skips when already at v14
- apply_migration() applies when at v13
- Version stamp reaches 14 after up-migration
- Down-migration drops all three tables + removes v14 stamp
- Down-migration preserves pre-existing tables (terminal_leases, runtime_schema_version)
- CHECK constraints enforced (scale_policy, state, provider)
- pool_config CHECK constraints (min/max/target bounds)
- UNIQUE constraints on (project_id, pool_id)
- Partial unique index: one active membership per terminal+project
- FK cascade: worker_pools row blocked when pool_config FK violated (FK=ON)
- FK: worker_pool_membership blocked on invalid pool_config
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_MIGRATION_DIR = Path(__file__).parent.parent / "schemas" / "migrations"
_UP_SQL_PATH = _MIGRATION_DIR / "0020_elastic_worker_pool.sql"
_DOWN_SQL_PATH = _MIGRATION_DIR / "0020_elastic_worker_pool_down.sql"

_TARGET_VERSION = 14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_db(schema_version: int = 13) -> sqlite3.Connection:
    """Return an in-memory DB with prerequisite tables set to schema_version."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(f"""
    CREATE TABLE runtime_schema_version (
        version     INTEGER PRIMARY KEY,
        description TEXT,
        applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    INSERT INTO runtime_schema_version(version, description)
    VALUES ({schema_version}, 'test base v{schema_version}');

    -- terminal_leases: referenced by worker_pool_membership FK
    CREATE TABLE terminal_leases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        terminal_id TEXT    NOT NULL,
        project_id  TEXT    NOT NULL,
        state       TEXT    NOT NULL DEFAULT 'free',
        lease_token TEXT    NOT NULL DEFAULT '',
        UNIQUE(terminal_id, project_id)
    );
    """)
    return conn


def _apply_up(conn: sqlite3.Connection) -> None:
    conn.executescript(_UP_SQL_PATH.read_text())


def _apply_down(conn: sqlite3.Connection) -> None:
    conn.executescript(_DOWN_SQL_PATH.read_text())


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------

def test_apply_creates_three_tables():
    conn = _base_db()
    _apply_up(conn)
    tables = _table_names(conn)
    assert "pool_config" in tables
    assert "worker_pools" in tables
    assert "worker_pool_membership" in tables


def test_apply_pool_config_columns():
    conn = _base_db()
    _apply_up(conn)
    cols = _column_names(conn, "pool_config")
    expected = {
        "id", "project_id", "pool_id", "min_workers", "max_workers",
        "target_workers", "role_mix_json", "provider_mix_json",
        "scale_policy", "cooldown_seconds", "created_at", "updated_at",
    }
    assert expected.issubset(cols)


def test_apply_worker_pools_columns():
    conn = _base_db()
    _apply_up(conn)
    cols = _column_names(conn, "worker_pools")
    expected = {
        "id", "project_id", "pool_id", "state", "current_size",
        "target_size", "healthy_count", "stuck_count", "last_scaled_at",
        "last_scale_action", "last_decision_json", "metadata_json",
    }
    assert expected.issubset(cols)


def test_apply_membership_columns():
    conn = _base_db()
    _apply_up(conn)
    cols = _column_names(conn, "worker_pool_membership")
    expected = {
        "id", "terminal_id", "project_id", "pool_id", "provider",
        "role", "joined_at", "released_at", "release_reason",
        "spawn_generation", "metadata_json",
    }
    assert expected.issubset(cols)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def test_apply_creates_indexes():
    conn = _base_db()
    _apply_up(conn)
    indexes = _index_names(conn)
    assert "idx_pool_config_project" in indexes
    assert "idx_worker_pools_state" in indexes
    assert "idx_worker_pools_project" in indexes
    assert "idx_pool_membership_active" in indexes
    assert "idx_pool_membership_pool" in indexes


# ---------------------------------------------------------------------------
# Bootstrap rows
# ---------------------------------------------------------------------------

def test_apply_inserts_default_pool_config_bootstrap():
    conn = _base_db()
    _apply_up(conn)
    row = conn.execute(
        "SELECT project_id, pool_id, min_workers, max_workers, target_workers "
        "FROM pool_config WHERE project_id='vnx-dev' AND pool_id='default'"
    ).fetchone()
    assert row is not None
    project_id, pool_id, min_w, max_w, target_w = row
    assert project_id == "vnx-dev"
    assert pool_id == "default"
    assert min_w == 1
    assert max_w == 4
    assert target_w == 3


def test_apply_inserts_default_worker_pools_bootstrap():
    conn = _base_db()
    _apply_up(conn)
    row = conn.execute(
        "SELECT project_id, pool_id, state, current_size, target_size "
        "FROM worker_pools WHERE project_id='vnx-dev' AND pool_id='default'"
    ).fetchone()
    assert row is not None
    _, _, state, current, target = row
    assert state == "idle"
    assert current == 0
    assert target == 3


def test_apply_bootstrap_provider_mix_matches_pr61_yaml():
    conn = _base_db()
    _apply_up(conn)
    row = conn.execute(
        "SELECT provider_mix_json FROM pool_config "
        "WHERE project_id='vnx-dev' AND pool_id='default'"
    ).fetchone()
    assert row is not None
    assert row[0] == '["claude"]'


# ---------------------------------------------------------------------------
# Version stamp
# ---------------------------------------------------------------------------

def test_version_stamped_to_14():
    conn = _base_db(schema_version=13)
    _apply_up(conn)
    row = conn.execute(
        "SELECT version FROM runtime_schema_version WHERE version=14"
    ).fetchone()
    assert row is not None
    assert row[0] == 14


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_apply_idempotent_sql_level():
    """Running up-migration SQL twice must not raise."""
    conn = _base_db()
    _apply_up(conn)
    # Second run: CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE guard this
    _apply_up(conn)
    # Only one v14 row should exist
    count = conn.execute(
        "SELECT COUNT(*) FROM runtime_schema_version WHERE version=14"
    ).fetchone()[0]
    assert count == 1


def test_apply_migration_fn_skips_when_already_v14(tmp_path):
    """apply_migration() returns False when DB already at v14."""
    from scripts.lib.migrations.apply_0020 import apply_migration

    db_path = tmp_path / "rc.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE runtime_schema_version (
        version INTEGER PRIMARY KEY,
        description TEXT,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    INSERT INTO runtime_schema_version(version, description) VALUES (14, 'already v14');
    """)
    conn.close()

    result = apply_migration(db_path, _UP_SQL_PATH, tmp_path)
    assert result is False


def test_apply_migration_fn_applies_when_at_v13(tmp_path):
    """apply_migration() returns True when DB is at v13."""
    from scripts.lib.migrations.apply_0020 import apply_migration

    db_path = tmp_path / "rc.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE runtime_schema_version (
        version INTEGER PRIMARY KEY,
        description TEXT,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    INSERT INTO runtime_schema_version(version, description) VALUES (13, 'v13 base');

    CREATE TABLE terminal_leases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        terminal_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'free',
        lease_token TEXT NOT NULL DEFAULT '',
        UNIQUE(terminal_id, project_id)
    );
    """)
    conn.close()

    result = apply_migration(db_path, _UP_SQL_PATH, tmp_path)
    assert result is True

    conn2 = sqlite3.connect(str(db_path))
    ver = conn2.execute(
        "SELECT MAX(version) FROM runtime_schema_version"
    ).fetchone()[0]
    assert ver == 14
    conn2.close()


# ---------------------------------------------------------------------------
# Down-migration
# ---------------------------------------------------------------------------

def test_down_migration_drops_all_three_tables():
    conn = _base_db()
    _apply_up(conn)
    _apply_down(conn)
    tables = _table_names(conn)
    assert "pool_config" not in tables
    assert "worker_pools" not in tables
    assert "worker_pool_membership" not in tables


def test_down_migration_drops_all_indexes():
    conn = _base_db()
    _apply_up(conn)
    _apply_down(conn)
    indexes = _index_names(conn)
    assert "idx_pool_config_project" not in indexes
    assert "idx_worker_pools_state" not in indexes
    assert "idx_worker_pools_project" not in indexes
    assert "idx_pool_membership_active" not in indexes
    assert "idx_pool_membership_pool" not in indexes


def test_down_migration_preserves_terminal_leases():
    conn = _base_db()
    conn.execute(
        "INSERT INTO terminal_leases(terminal_id, project_id) VALUES ('T1', 'proj-a')"
    )
    conn.commit()
    _apply_up(conn)
    _apply_down(conn)
    assert "terminal_leases" in _table_names(conn)
    count = conn.execute("SELECT COUNT(*) FROM terminal_leases").fetchone()[0]
    assert count == 1


def test_down_migration_preserves_runtime_schema_version():
    conn = _base_db(schema_version=13)
    _apply_up(conn)
    _apply_down(conn)
    assert "runtime_schema_version" in _table_names(conn)


def test_down_migration_removes_v14_stamp():
    conn = _base_db()
    _apply_up(conn)
    _apply_down(conn)
    row = conn.execute(
        "SELECT version FROM runtime_schema_version WHERE version=14"
    ).fetchone()
    assert row is None


def test_down_migration_preserves_lower_version_stamps():
    conn = _base_db(schema_version=13)
    _apply_up(conn)
    _apply_down(conn)
    row = conn.execute(
        "SELECT version FROM runtime_schema_version WHERE version=13"
    ).fetchone()
    assert row is not None


def test_apply_down_migration_fn_skips_when_below_v14(tmp_path):
    """apply_down_migration() returns False when DB is below v14."""
    from scripts.lib.migrations.apply_0020 import apply_down_migration

    db_path = tmp_path / "rc.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE runtime_schema_version (
        version INTEGER PRIMARY KEY,
        description TEXT,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    INSERT INTO runtime_schema_version(version, description) VALUES (13, 'v13');
    """)
    conn.close()

    result = apply_down_migration(db_path, _DOWN_SQL_PATH, tmp_path)
    assert result is False


# ---------------------------------------------------------------------------
# CHECK constraints — pool_config
# ---------------------------------------------------------------------------

def test_check_min_workers_negative_rejected():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
            "VALUES ('p1', 'bad', -1, 4, 2)"
        )
        conn.commit()


def test_check_max_less_than_min_rejected():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
            "VALUES ('p1', 'bad', 4, 2, 3)"
        )
        conn.commit()


def test_check_target_outside_min_max_rejected():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
            "VALUES ('p1', 'bad', 1, 4, 10)"
        )
        conn.commit()


def test_check_valid_pool_config_accepted():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
        "VALUES ('proj-x', 'default', 1, 6, 3)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT project_id FROM pool_config WHERE project_id='proj-x'"
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# CHECK constraints — worker_pools state enum
# ---------------------------------------------------------------------------

def test_check_worker_pools_state_invalid_rejected():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pools(project_id, pool_id, state) "
            "VALUES ('p1', 'default', 'unknown_state')"
        )
        conn.commit()


def test_check_worker_pools_all_valid_states_accepted():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    for i, state in enumerate(("idle", "scaling", "draining", "quota_exhausted")):
        conn.execute(
            "INSERT INTO worker_pools(project_id, pool_id, state) "
            "VALUES (?, ?, ?)",
            (f"proj-{i}", state, state),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# CHECK constraints — worker_pool_membership provider enum
# ---------------------------------------------------------------------------

def test_check_membership_invalid_provider_rejected():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
            "VALUES ('T1', 'p1', 'default', 'openai', 'backend-developer')"
        )
        conn.commit()


def test_check_membership_all_valid_providers_accepted():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    for i, provider in enumerate(("claude", "codex", "gemini", "litellm")):
        conn.execute(
            "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
            "VALUES (?, 'p1', 'default', ?, 'backend-developer')",
            (f"T{i}", provider),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# UNIQUE constraints
# ---------------------------------------------------------------------------

def test_unique_pool_config_project_pool_id():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
        "VALUES ('dup-proj', 'default', 1, 4, 2)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
            "VALUES ('dup-proj', 'default', 2, 6, 3)"
        )
        conn.commit()


def test_unique_worker_pools_project_pool_id():
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO worker_pools(project_id, pool_id) VALUES ('dup-proj', 'default')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pools(project_id, pool_id) VALUES ('dup-proj', 'default')"
        )
        conn.commit()


def test_pool_membership_partial_unique_active_per_terminal():
    """Two active rows for same terminal+project must be rejected."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
        "VALUES ('T1', 'proj-a', 'default', 'claude', 'backend-developer')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
            "VALUES ('T1', 'proj-a', 'default', 'codex', 'quality-engineer')"
        )
        conn.commit()


def test_pool_membership_released_rows_allow_new_active():
    """After releasing a membership, a new active row for same terminal is allowed."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    # Insert and release first membership
    conn.execute(
        "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role, released_at) "
        "VALUES ('T1', 'proj-a', 'default', 'claude', 'backend-developer', '2026-05-16T00:00:00Z')"
    )
    conn.commit()
    # New active membership for same terminal should succeed (released_at IS NULL partial idx)
    conn.execute(
        "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
        "VALUES ('T1', 'proj-a', 'default', 'claude', 'backend-developer')"
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE terminal_id='T1'"
    ).fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Foreign key enforcement
# ---------------------------------------------------------------------------

def test_worker_pools_fk_rejects_missing_pool_config():
    """worker_pools FK to pool_config must reject orphaned inserts."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pools(project_id, pool_id) "
            "VALUES ('nonexistent-project', 'nonexistent-pool')"
        )
        conn.commit()


def test_membership_fk_rejects_missing_pool_config():
    """worker_pool_membership FK to pool_config must reject orphaned inserts."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    # Insert a valid terminal_leases row
    conn.execute(
        "INSERT INTO terminal_leases(terminal_id, project_id) VALUES ('T9', 'proj-b')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
            "VALUES ('T9', 'proj-b', 'nonexistent-pool', 'claude', 'backend-developer')"
        )
        conn.commit()


def test_membership_fk_rejects_missing_terminal_lease():
    """worker_pool_membership FK to terminal_leases must reject orphaned inserts."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    # Ensure pool_config exists for the FK
    conn.execute(
        "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
        "VALUES ('proj-c', 'default', 1, 4, 2)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
            "VALUES ('ghost-terminal', 'proj-c', 'default', 'claude', 'backend-developer')"
        )
        conn.commit()


def test_valid_membership_insert_with_all_fks():
    """A fully valid membership insert should succeed when all FKs are satisfied."""
    conn = _base_db()
    _apply_up(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO terminal_leases(terminal_id, project_id) VALUES ('T5', 'proj-d')"
    )
    conn.execute(
        "INSERT INTO pool_config(project_id, pool_id, min_workers, max_workers, target_workers) "
        "VALUES ('proj-d', 'default', 1, 4, 2)"
    )
    conn.commit()
    conn.execute(
        "INSERT INTO worker_pool_membership(terminal_id, project_id, pool_id, provider, role) "
        "VALUES ('T5', 'proj-d', 'default', 'claude', 'backend-developer')"
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE terminal_id='T5'"
    ).fetchone()[0]
    assert count == 1
