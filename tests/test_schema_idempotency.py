"""Tests for idempotent schema bootstrap via PRAGMA user_version.

Covers:
1. Fresh DB bootstrap → all migrations applied, user_version = HIGHEST_QI_VERSION
2. Already-bootstrapped DB → 0 new migrations, user_version unchanged
3. DB at user_version=9 → remaining migrations applied, user_version = HIGHEST_QI_VERSION
4. Mid-migration failure → transaction rollback, user_version unchanged
5. Repeated bootstrap (5×) → user_version stays at HIGHEST_QI_VERSION after first run
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_SCRIPTS, _LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quality_db_init import bootstrap_qi_db, HIGHEST_QI_VERSION
from schema_migration import apply_if_below, get_user_version
import coordination_db

_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "quality_intelligence.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uv(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return v


def _set_uv(db_path: Path, version: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: fresh DB → all migrations applied
# ---------------------------------------------------------------------------

def test_bootstrap_fresh_db_applies_all_migrations(tmp_path):
    """Bootstrap on an empty DB must apply every migration and reach HIGHEST_QI_VERSION."""
    db = tmp_path / "qi_fresh.db"
    assert not db.exists()

    result = bootstrap_qi_db(db, schema_file=_SCHEMA_FILE)

    assert result is True, "bootstrap_qi_db must return True on success"
    assert db.exists(), "DB file must be created"
    assert _uv(db) == HIGHEST_QI_VERSION, (
        f"Expected user_version={HIGHEST_QI_VERSION}, got {_uv(db)}"
    )

    # Spot-check key tables created by inline migrations
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    for tbl in (
        "session_analytics",
        "report_findings",
        "governance_metrics",
        "confidence_events",
        "dispatch_pattern_offered",
    ):
        assert tbl in tables, f"Table '{tbl}' missing after bootstrap"


# ---------------------------------------------------------------------------
# Test 2: already-bootstrapped DB → 0 new migrations
# ---------------------------------------------------------------------------

def test_bootstrap_already_complete_skips_all_migrations(tmp_path):
    """Second bootstrap on a fully-migrated DB must be a no-op."""
    db = tmp_path / "qi_complete.db"

    # Initial bootstrap
    assert bootstrap_qi_db(db, schema_file=_SCHEMA_FILE) is True
    assert _uv(db) == HIGHEST_QI_VERSION

    # Second bootstrap — user_version must not change
    result = bootstrap_qi_db(db, schema_file=_SCHEMA_FILE)

    assert result is True
    assert _uv(db) == HIGHEST_QI_VERSION, "user_version must remain unchanged on re-bootstrap"


# ---------------------------------------------------------------------------
# Test 3: DB at user_version=9 → only remaining migrations applied
# ---------------------------------------------------------------------------

def test_bootstrap_partial_db_applies_remaining_migrations(tmp_path):
    """DB at user_version=9 must have migrations 10-HIGHEST applied; 1-9 skipped."""
    db = tmp_path / "qi_v9.db"

    # Build a fully-bootstrapped DB so all tables/columns exist
    assert bootstrap_qi_db(db, schema_file=_SCHEMA_FILE) is True
    assert _uv(db) == HIGHEST_QI_VERSION

    # Simulate a v9 state (roll back user_version stamp, schema unchanged)
    _set_uv(db, 9)
    assert _uv(db) == 9

    # Re-bootstrap should advance from 9 to HIGHEST_QI_VERSION
    result = bootstrap_qi_db(db, schema_file=_SCHEMA_FILE)

    assert result is True
    assert _uv(db) == HIGHEST_QI_VERSION, (
        f"Expected user_version={HIGHEST_QI_VERSION} after re-bootstrap from 9, got {_uv(db)}"
    )


# ---------------------------------------------------------------------------
# Test 4: mid-migration failure → rollback, user_version unchanged
# ---------------------------------------------------------------------------

def test_apply_if_below_rolls_back_on_failure():
    """apply_if_below must roll back and leave user_version unchanged on migration failure."""
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    # Run a successful migration first to get to version 1
    def _ok(c):
        c.execute("ALTER TABLE t ADD COLUMN a TEXT")
    assert apply_if_below(conn, 1, _ok) is True
    assert get_user_version(conn) == 1

    # Now attempt a failing migration
    def _fail(c):
        c.execute("ALTER TABLE t ADD COLUMN b TEXT")
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        apply_if_below(conn, 2, _fail)

    # user_version must still be 1 (rolled back)
    assert get_user_version(conn) == 1, (
        "user_version must not advance when migration_fn raises"
    )

    # Column 'b' must not exist (transaction rolled back)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()}
    assert "b" not in cols, "Rolled-back column 'b' must not appear in schema"

    conn.close()


# ---------------------------------------------------------------------------
# Test 5: repeated bootstrap is idempotent (5 runs)
# ---------------------------------------------------------------------------

def test_bootstrap_idempotent_repeated_runs(tmp_path):
    """Calling bootstrap_qi_db 5 times on the same DB must produce identical final state."""
    db = tmp_path / "qi_repeat.db"

    for run in range(5):
        result = bootstrap_qi_db(db, schema_file=_SCHEMA_FILE)
        assert result is True, f"bootstrap_qi_db failed on run {run + 1}"
        assert _uv(db) == HIGHEST_QI_VERSION, (
            f"user_version drifted on run {run + 1}: expected {HIGHEST_QI_VERSION}, got {_uv(db)}"
        )


# ---------------------------------------------------------------------------
# Tests for coordination_db._needs_initial_migration legacy fallback
# ---------------------------------------------------------------------------

def test_needs_initial_migration_fresh_db():
    """Fresh DB (PRAGMA=0, no runtime_schema_version table) → needs migration."""
    conn = sqlite3.connect(":memory:")
    assert coordination_db._needs_initial_migration(conn) is True
    conn.close()


def test_needs_initial_migration_pragma_high():
    """PRAGMA user_version=10 → no migration needed."""
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("PRAGMA user_version = 10")
    assert coordination_db._needs_initial_migration(conn) is False
    conn.close()


def test_needs_initial_migration_legacy_table_syncs_pragma():
    """PRAGMA=0 but runtime_schema_version table has v9 → no migration + PRAGMA synced to 9."""
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("""
        CREATE TABLE runtime_schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO runtime_schema_version (version, applied_at) VALUES (9, '2026-01-01')"
    )
    assert get_user_version(conn) == 0
    assert coordination_db._needs_initial_migration(conn) is False
    assert get_user_version(conn) == 9
    conn.close()
