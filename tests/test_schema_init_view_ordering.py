"""Regression tests for schema-init failure mode 2: view-ordering on legacy DBs.

Root cause: on a DB at user_version=21, the apply_script_if_below(conn, 1, schema_sql)
call is skipped (version >= 1), leaving the three dispatch_metadata-dependent views
already in the DB.  _migrate_v22 drops and recreates the dispatch_metadata table via
DROP TABLE + RENAME.  SQLite validates dependent views on the RENAME step and threw:
    "error in view dispatch_success_by_role: no such table: main.dispatch_metadata"
because the table was still absent (dropped but not yet renamed back).

Fix: _migrate_v22 drops the three views before DROP TABLE and recreates them after RENAME.

Covers:
- Scenario A: legacy DB at user_version=21 (live production state) bootstraps clean
- Scenario B: fresh empty DB bootstraps clean (no regression)
- Scenario C: already-at-v23 DB bootstraps idempotently (no double-apply)
- Data preservation: existing dispatch_metadata rows survive the v22 rebuild
- View integrity: all three views are queryable after bootstrap on the legacy DB
- ADR-007: composite UNIQUE(project_id, dispatch_id) on dispatch_metadata after bootstrap
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import quality_db_init as QDB  # noqa: E402
import schema_migration  # noqa: E402

_SCHEMA_FILE = REPO_ROOT / "schemas" / "quality_intelligence.sql"

_DISPATCH_METADATA_V21_DDL = """
    CREATE TABLE IF NOT EXISTS dispatch_metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispatch_id TEXT NOT NULL UNIQUE,
        terminal TEXT NOT NULL,
        track TEXT NOT NULL,
        role TEXT,
        skill_name TEXT,
        gate TEXT,
        cognition TEXT DEFAULT 'normal',
        priority TEXT DEFAULT 'P1',
        pr_id TEXT,
        parent_dispatch TEXT,
        pattern_count INTEGER DEFAULT 0,
        prevention_rule_count INTEGER DEFAULT 0,
        intelligence_json TEXT,
        instruction_char_count INTEGER DEFAULT 0,
        context_file_count INTEGER DEFAULT 0,
        dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        completed_at DATETIME,
        outcome_status TEXT,
        outcome_report_path TEXT,
        session_id TEXT,
        cqs REAL,
        normalized_status TEXT,
        cqs_components TEXT,
        target_open_items TEXT,
        open_items_created INTEGER DEFAULT 0,
        open_items_resolved INTEGER DEFAULT 0,
        quality_advisory_json TEXT,
        provider TEXT
    )
"""

_DISPATCH_SUCCESS_VIEW = """
    CREATE VIEW IF NOT EXISTS dispatch_success_by_role AS
    SELECT role, COUNT(*) as total_dispatches,
           SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
           ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
           AVG(pattern_count) as avg_patterns,
           AVG(prevention_rule_count) as avg_rules,
           AVG(instruction_char_count) as avg_instruction_chars
    FROM dispatch_metadata WHERE outcome_status IS NOT NULL GROUP BY role
    ORDER BY total_dispatches DESC
"""

_INTELLIGENCE_EFFECTIVENESS_VIEW = """
    CREATE VIEW IF NOT EXISTS intelligence_effectiveness AS
    SELECT
        CASE WHEN intelligence_json IS NOT NULL AND intelligence_json != '' THEN 'with_intelligence' ELSE 'without_intelligence' END as intelligence_used,
        COUNT(*) as total,
        SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
        ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
        AVG(pattern_count) as avg_patterns
    FROM dispatch_metadata WHERE outcome_status IS NOT NULL GROUP BY intelligence_used
"""

_COST_PER_DISPATCH_VIEW = """
    CREATE VIEW IF NOT EXISTS cost_per_dispatch AS
    SELECT
        dm.dispatch_id, dm.terminal, dm.role, dm.gate, dm.outcome_status,
        NULL as session_model, NULL as total_input_tokens, NULL as total_output_tokens,
        NULL as tool_calls_total, NULL as duration_minutes,
        dm.pattern_count, dm.instruction_char_count
    FROM dispatch_metadata dm WHERE dm.outcome_status IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_realistic_v21_db(path: Path, row_count: int = 3) -> None:
    """Build a v21 DB that closely matches real production state.

    Includes session_analytics (created by _migrate_v3 in production) alongside
    dispatch_metadata + three views, all at user_version=21.  Used for tests that
    need the full LEFT JOIN in cost_per_dispatch to succeed.
    """
    _make_legacy_v21_db(path, row_count)
    conn = sqlite3.connect(str(path))
    try:
        conn.isolation_level = None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                project_path TEXT NOT NULL,
                terminal TEXT,
                session_date DATE NOT NULL,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                tool_calls_total INTEGER DEFAULT 0,
                duration_minutes REAL,
                session_model TEXT DEFAULT 'unknown',
                dispatch_id TEXT,
                analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Keep user_version at 21
        conn.execute("PRAGMA user_version = 21")
    finally:
        conn.close()


def _make_legacy_v21_db(path: Path, row_count: int = 3) -> None:
    """Build a DB that mirrors the live production state at user_version=21.

    Reproduces the exact schema state:
    - dispatch_metadata with UNIQUE(dispatch_id) only (no project_id column)
    - three views that reference dispatch_metadata
    - user_version=21
    The base schema tables (vnx_code_quality, session_analytics, etc.) are NOT
    present — this simulates a DB bootstrapped on an older version of the schema
    that grew via incremental ALTER TABLE migrations.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.isolation_level = None
        conn.execute(_DISPATCH_METADATA_V21_DDL)
        conn.execute(_DISPATCH_SUCCESS_VIEW)
        conn.execute(_INTELLIGENCE_EFFECTIVENESS_VIEW)
        conn.execute(_COST_PER_DISPATCH_VIEW)
        for i in range(row_count):
            conn.execute(
                "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, role, outcome_status) "
                "VALUES (?, 'T1', 'A', 'backend-developer', 'success')",
                (f"legacy-dispatch-{i:04d}",),
            )
        conn.execute(f"PRAGMA user_version = 21")
    finally:
        conn.close()


def _get_user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _table_exists(db_path: Path, name: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone())
    finally:
        conn.close()


def _view_exists(db_path: Path, name: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (name,)
        ).fetchone())
    finally:
        conn.close()


def _row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _dispatch_metadata_has_composite_unique(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        tbl_sql = (conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatch_metadata'"
        ).fetchone() or ("",))[0]
        return (
            "UNIQUE (project_id, dispatch_id)" in tbl_sql
            or "UNIQUE(project_id,dispatch_id)" in tbl_sql
            or "UNIQUE(project_id, dispatch_id)" in tbl_sql
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario A — legacy DB at user_version=21 (production reproduction)
# ---------------------------------------------------------------------------

class TestLegacyV21Bootstrap:
    """Scenario A: the exact legacy DB state that was failing in production."""

    def test_bootstrap_succeeds_on_v21_legacy_db(self, tmp_path):
        """bootstrap_qi_db must return True on a DB at user_version=21."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db, row_count=3)
        assert _get_user_version(db) == 21, "precondition: legacy DB at v21"

        result = QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        assert result is True, (
            "bootstrap_qi_db must return True on a legacy v21 DB — "
            "this was the production failure (failure mode 2)"
        )

    def test_legacy_db_reaches_highest_version(self, tmp_path):
        """user_version must be HIGHEST_QI_VERSION after bootstrap on v21 DB."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        version = _get_user_version(db)
        assert version == QDB.HIGHEST_QI_VERSION, (
            f"user_version must be {QDB.HIGHEST_QI_VERSION} after bootstrap, got {version}"
        )

    def test_dispatch_metadata_rows_preserved_through_v22_rebuild(self, tmp_path):
        """Existing dispatch_metadata rows must survive the v22 table rebuild."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db, row_count=5)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        count = _row_count(db, "dispatch_metadata")
        assert count == 5, (
            f"dispatch_metadata must retain 5 rows after v22 rebuild, got {count}"
        )

    def test_dispatch_metadata_composite_unique_after_v22(self, tmp_path):
        """ADR-007: dispatch_metadata must have UNIQUE(project_id, dispatch_id) after bootstrap."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        assert _dispatch_metadata_has_composite_unique(db), (
            "dispatch_metadata must have composite UNIQUE(project_id, dispatch_id) "
            "after v22 migration (ADR-007)"
        )

    def test_dispatch_success_by_role_view_queryable_after_legacy_bootstrap(self, tmp_path):
        """dispatch_success_by_role must be queryable after bootstrap on legacy DB."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db, row_count=2)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        assert _view_exists(db, "dispatch_success_by_role"), (
            "dispatch_success_by_role view must exist after bootstrap"
        )
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT * FROM dispatch_success_by_role").fetchall()
        finally:
            conn.close()

    def test_intelligence_effectiveness_view_queryable_after_legacy_bootstrap(self, tmp_path):
        """intelligence_effectiveness must be queryable after bootstrap on legacy DB."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db, row_count=2)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        assert _view_exists(db, "intelligence_effectiveness"), (
            "intelligence_effectiveness view must exist after bootstrap"
        )
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("SELECT * FROM intelligence_effectiveness").fetchall()
        finally:
            conn.close()

    def test_cost_per_dispatch_view_queryable_after_legacy_bootstrap(self, tmp_path):
        """cost_per_dispatch must exist and be queryable after bootstrap on legacy DB.

        The view uses LEFT JOIN session_analytics.  The real production v21 DB already
        has session_analytics (created by _migrate_v3 during the original bootstrap).
        We use _make_realistic_v21_db to match that state.
        """
        db = tmp_path / "qi_v21_real.db"
        _make_realistic_v21_db(db, row_count=2)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        assert _view_exists(db, "cost_per_dispatch"), (
            "cost_per_dispatch view must exist after bootstrap"
        )
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("SELECT * FROM cost_per_dispatch").fetchall()
        finally:
            conn.close()

    def test_legacy_bootstrap_idempotent(self, tmp_path):
        """Running bootstrap twice on a v21 legacy DB must be a clean no-op."""
        db = tmp_path / "qi_v21.db"
        _make_legacy_v21_db(db, row_count=3)

        assert QDB.bootstrap_qi_db(db, _SCHEMA_FILE) is True, "first bootstrap must succeed"
        assert QDB.bootstrap_qi_db(db, _SCHEMA_FILE) is True, "second bootstrap must succeed"

        assert _get_user_version(db) == QDB.HIGHEST_QI_VERSION
        assert _row_count(db, "dispatch_metadata") == 3


# ---------------------------------------------------------------------------
# Scenario B — fresh empty DB (no regression)
# ---------------------------------------------------------------------------

class TestFreshDbBootstrap:
    """Scenario B: fresh empty DB must bootstrap without regression."""

    def test_fresh_db_bootstrap_succeeds(self, tmp_path):
        """bootstrap_qi_db must return True on a completely fresh DB."""
        db = tmp_path / "qi_fresh.db"
        result = QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert result is True

    def test_fresh_db_reaches_highest_version(self, tmp_path):
        """Fresh DB must reach HIGHEST_QI_VERSION after bootstrap."""
        db = tmp_path / "qi_fresh.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert _get_user_version(db) == QDB.HIGHEST_QI_VERSION

    def test_fresh_db_has_dispatch_metadata(self, tmp_path):
        """dispatch_metadata must exist after fresh bootstrap."""
        db = tmp_path / "qi_fresh.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert _table_exists(db, "dispatch_metadata")

    def test_fresh_db_has_composite_unique(self, tmp_path):
        """ADR-007: fresh DB must have UNIQUE(project_id, dispatch_id) from base schema."""
        db = tmp_path / "qi_fresh.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert _dispatch_metadata_has_composite_unique(db), (
            "dispatch_metadata must have composite UNIQUE(project_id, dispatch_id) "
            "on fresh install (base schema, ADR-007)"
        )

    def test_fresh_db_all_three_views_exist(self, tmp_path):
        """All three dispatch_metadata views must exist after fresh bootstrap."""
        db = tmp_path / "qi_fresh.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        for view in ("dispatch_success_by_role", "intelligence_effectiveness", "cost_per_dispatch"):
            assert _view_exists(db, view), f"{view} must exist after fresh bootstrap"

    def test_fresh_db_bootstrap_idempotent(self, tmp_path):
        """Running bootstrap twice on a fresh DB must be a clean no-op."""
        db = tmp_path / "qi_fresh.db"
        assert QDB.bootstrap_qi_db(db, _SCHEMA_FILE) is True
        assert QDB.bootstrap_qi_db(db, _SCHEMA_FILE) is True
        assert _get_user_version(db) == QDB.HIGHEST_QI_VERSION


# ---------------------------------------------------------------------------
# Scenario C — already-at-v23 DB (idempotency)
# ---------------------------------------------------------------------------

class TestAlreadyCurrentDbBootstrap:
    """Scenario C: a DB already at the current version must be a no-op."""

    def test_already_current_db_bootstrap_succeeds(self, tmp_path):
        """bootstrap_qi_db on a DB already at HIGHEST_QI_VERSION must return True."""
        db = tmp_path / "qi_current.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert _get_user_version(db) == QDB.HIGHEST_QI_VERSION

        result = QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        assert result is True

    def test_already_current_db_version_unchanged(self, tmp_path):
        """Re-running bootstrap on a current DB must leave user_version unchanged."""
        db = tmp_path / "qi_current.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        before = _get_user_version(db)

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        after = _get_user_version(db)

        assert before == after == QDB.HIGHEST_QI_VERSION

    def test_already_current_db_data_preserved(self, tmp_path):
        """Rows inserted after first bootstrap survive re-bootstrap."""
        db = tmp_path / "qi_current.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, terminal, track) "
            "VALUES ('idempotent-test-dispatch', 'T1', 'A')"
        )
        conn.commit()
        conn.close()

        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT dispatch_id FROM dispatch_metadata WHERE dispatch_id='idempotent-test-dispatch'"
        ).fetchone()
        conn.close()

        assert row is not None, "dispatch_metadata row must survive re-bootstrap"

    def test_already_current_db_views_queryable(self, tmp_path):
        """All three views must remain queryable after re-bootstrap on current DB."""
        db = tmp_path / "qi_current.db"
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)
        QDB.bootstrap_qi_db(db, _SCHEMA_FILE)

        conn = sqlite3.connect(str(db))
        try:
            for view in ("dispatch_success_by_role", "intelligence_effectiveness", "cost_per_dispatch"):
                conn.execute(f"SELECT * FROM {view}").fetchall()
        finally:
            conn.close()
