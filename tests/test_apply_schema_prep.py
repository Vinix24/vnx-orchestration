"""Tests for scripts/apply_schema_prep.py.

Covers:
  - Apply on fresh DB: all ALTERs execute (column added)
  - Apply on partially-prepped DB: only missing columns added
  - Apply on fully-prepped DB: no-op (all statements skipped)
  - Post-apply PRAGMA table_info matches expected columns
  - CREATE TABLE IF NOT EXISTS is idempotent
  - Dry-run: no writes happen regardless of DB state
  - Error path: non-existent source_db_dir returns error, not exception
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.apply_schema_prep import (  # noqa: E402
    PrepResult,
    StatementResult,
    _apply_statements_to_db,
    _column_exists,
    _split_sql_statements,
    _table_exists,
    apply_prep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_PREP_SQL = """\
-- Test prep: add project_id to test_table, create missing_table
ALTER TABLE test_table ADD COLUMN project_id TEXT NOT NULL DEFAULT 'test-project';
CREATE TABLE IF NOT EXISTS missing_table (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'test-project'
);
CREATE INDEX IF NOT EXISTS idx_missing_table_project ON missing_table (project_id);
"""


def _make_db_with_table(path: Path, with_project_id: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cols = "id INTEGER PRIMARY KEY, name TEXT"
    if with_project_id:
        cols += ", project_id TEXT NOT NULL DEFAULT 'test-project'"
    con.executescript(f"CREATE TABLE IF NOT EXISTS test_table ({cols});")
    con.commit()
    con.close()
    return path


def _get_columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    con.close()
    return cols


# ---------------------------------------------------------------------------
# Tests: _split_sql_statements
# ---------------------------------------------------------------------------


def test_split_ignores_comment_lines():
    sql = "-- comment\nALTER TABLE foo ADD COLUMN bar TEXT; -- inline\nCREATE TABLE IF NOT EXISTS baz (id INT);"
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 2
    assert any("ALTER TABLE" in s for s in stmts)
    assert any("CREATE TABLE" in s for s in stmts)


def test_split_skips_empty():
    stmts = _split_sql_statements("   ;  ;  -- just comments\n")
    assert stmts == []


# ---------------------------------------------------------------------------
# Tests: _apply_statements_to_db — fresh DB
# ---------------------------------------------------------------------------


def test_apply_on_fresh_db_adds_column(tmp_path: Path):
    """Fresh DB without project_id: ALTER TABLE executes and adds the column."""
    db = tmp_path / "test.db"
    _make_db_with_table(db, with_project_id=False)

    stmts = _split_sql_statements(_MINIMAL_PREP_SQL)
    results, errors = _apply_statements_to_db(db, stmts, dry_run=False, project_id="test-project")

    assert errors == []
    cols = _get_columns(db, "test_table")
    assert "project_id" in cols

    applied = [r for r in results if r.action == "applied"]
    assert any(r.stmt_type == "alter_table" for r in applied)


def test_apply_on_fresh_db_creates_table(tmp_path: Path):
    """Fresh DB: CREATE TABLE IF NOT EXISTS creates the missing_table."""
    db = tmp_path / "test.db"
    _make_db_with_table(db, with_project_id=False)

    stmts = _split_sql_statements(_MINIMAL_PREP_SQL)
    _apply_statements_to_db(db, stmts, dry_run=False, project_id="test-project")

    con = sqlite3.connect(db)
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='missing_table'"
    ).fetchone()
    con.close()
    assert exists is not None


# ---------------------------------------------------------------------------
# Tests: _apply_statements_to_db — partially prepped
# ---------------------------------------------------------------------------


def test_apply_on_partial_db_skips_existing_column(tmp_path: Path):
    """DB already has project_id: ALTER TABLE is skipped, not applied twice."""
    db = tmp_path / "test.db"
    _make_db_with_table(db, with_project_id=True)  # already has project_id

    stmts = _split_sql_statements(_MINIMAL_PREP_SQL)
    results, errors = _apply_statements_to_db(db, stmts, dry_run=False, project_id="test-project")

    assert errors == []
    skipped = [r for r in results if r.action == "skipped" and r.stmt_type == "alter_table"]
    assert len(skipped) == 1
    assert skipped[0].detail == "column already exists"


# ---------------------------------------------------------------------------
# Tests: _apply_statements_to_db — fully prepped (no-op)
# ---------------------------------------------------------------------------


def test_fully_prepped_db_is_noop(tmp_path: Path):
    """Run twice: second run is a complete no-op (0 applied, all skipped)."""
    db = tmp_path / "test.db"
    _make_db_with_table(db, with_project_id=False)

    stmts = _split_sql_statements(_MINIMAL_PREP_SQL)
    # First run
    _apply_statements_to_db(db, stmts, dry_run=False, project_id="test-project")
    # Second run
    results, errors = _apply_statements_to_db(db, stmts, dry_run=False, project_id="test-project")

    assert errors == []
    applied = [r for r in results if r.action == "applied" and r.stmt_type == "alter_table"]
    assert applied == []


# ---------------------------------------------------------------------------
# Tests: dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(tmp_path: Path):
    """Dry-run mode: DB is unmodified after call."""
    db = tmp_path / "test.db"
    _make_db_with_table(db, with_project_id=False)

    stmts = _split_sql_statements(_MINIMAL_PREP_SQL)
    results, errors = _apply_statements_to_db(db, stmts, dry_run=True, project_id="test-project")

    assert errors == []
    cols = _get_columns(db, "test_table")
    assert "project_id" not in cols  # NOT written

    dry_run_stmts = [r for r in results if r.action == "dry_run"]
    assert len(dry_run_stmts) > 0


# ---------------------------------------------------------------------------
# Tests: apply_prep (full integration via prep SQL files)
# ---------------------------------------------------------------------------


def test_apply_prep_sales_copilot_fresh(tmp_path: Path):
    """apply_prep for sales-copilot on minimal fresh DBs: columns added."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Create minimal versions of the 3 DBs with the tables that get ALTERed
    qi = state_dir / "quality_intelligence.db"
    con = sqlite3.connect(qi)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS prevention_rules (id INTEGER PRIMARY KEY, tag_combination TEXT);
        CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
            dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT, offered_at TEXT,
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        CREATE TABLE IF NOT EXISTS session_analytics (id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE IF NOT EXISTS confidence_events (id INTEGER PRIMARY KEY, dispatch_id TEXT, outcome TEXT NOT NULL, confidence_change REAL NOT NULL, occurred_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS success_patterns (id INTEGER PRIMARY KEY, pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, pattern_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS antipatterns (id INTEGER PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, why_problematic TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT, terminal TEXT NOT NULL, track TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pattern_usage (pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL);
    """)
    con.commit()
    con.close()

    rc = state_dir / "runtime_coordination.db"
    con = sqlite3.connect(rc)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (id INTEGER PRIMARY KEY, attempt_id TEXT, dispatch_id TEXT);
        CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS terminal_leases (id INTEGER PRIMARY KEY, terminal_id TEXT);
        CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY, event_id TEXT, entity_type TEXT, entity_id TEXT);
        CREATE TABLE IF NOT EXISTS intelligence_injections (id INTEGER PRIMARY KEY, injection_id TEXT, dispatch_id TEXT);
    """)
    con.commit()
    con.close()

    # dispatch_tracker.db — empty (dispatch_experiments table to be created)
    (state_dir / "dispatch_tracker.db").touch()

    result = apply_prep("sales-copilot", state_dir, dry_run=False)

    assert result.errors == [], f"Errors: {result.errors}"

    # Verify project_id exists in key tables
    qi_cols = _get_columns(qi, "prevention_rules")
    assert "project_id" in qi_cols

    rc_cols = _get_columns(rc, "dispatches")
    assert "project_id" in rc_cols

    # dispatch_experiments must have been created
    dt = state_dir / "dispatch_tracker.db"
    dt_con = sqlite3.connect(dt)
    exists = dt_con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_experiments'"
    ).fetchone()
    dt_con.close()
    assert exists is not None


def test_apply_prep_seocrawler_v2_creates_missing_tables(tmp_path: Path):
    """apply_prep for seocrawler-v2: creates confidence_events + dispatch_pattern_offered."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    qi = state_dir / "quality_intelligence.db"
    con = sqlite3.connect(qi)
    # Do NOT create confidence_events or dispatch_pattern_offered (they're missing in SEO)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS prevention_rules (id INTEGER PRIMARY KEY, tag_combination TEXT);
        CREATE TABLE IF NOT EXISTS session_analytics (id INTEGER PRIMARY KEY, session_id TEXT);
        CREATE TABLE IF NOT EXISTS success_patterns (id INTEGER PRIMARY KEY, pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, pattern_data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS antipatterns (id INTEGER PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, why_problematic TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT, terminal TEXT NOT NULL, track TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pattern_usage (pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL);
    """)
    con.commit()
    con.close()

    rc = state_dir / "runtime_coordination.db"
    con = sqlite3.connect(rc)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (id INTEGER PRIMARY KEY, attempt_id TEXT, dispatch_id TEXT);
        CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS terminal_leases (id INTEGER PRIMARY KEY, terminal_id TEXT);
        CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY, event_id TEXT, entity_type TEXT, entity_id TEXT);
        CREATE TABLE IF NOT EXISTS intelligence_injections (id INTEGER PRIMARY KEY, injection_id TEXT, dispatch_id TEXT);
    """)
    con.commit()
    con.close()

    (state_dir / "dispatch_tracker.db").touch()

    result = apply_prep("seocrawler-v2", state_dir, dry_run=False)
    assert result.errors == [], f"Errors: {result.errors}"

    qi_con = sqlite3.connect(qi)
    for tbl in ("confidence_events", "dispatch_pattern_offered"):
        exists = qi_con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        assert exists is not None, f"Table {tbl} was not created"
    qi_con.close()


def test_apply_prep_fully_prepped_is_noop(tmp_path: Path):
    """Second apply_prep on already-prepped DB: 0 ALTERs applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    qi = state_dir / "quality_intelligence.db"
    con = sqlite3.connect(qi)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY, tag_combination TEXT,
            project_id TEXT NOT NULL DEFAULT 'sales-copilot'
        );
        CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
            dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT, offered_at TEXT,
            project_id TEXT NOT NULL DEFAULT 'sales-copilot',
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        CREATE TABLE IF NOT EXISTS session_analytics (id INTEGER PRIMARY KEY, session_id TEXT, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS confidence_events (id INTEGER PRIMARY KEY, dispatch_id TEXT, outcome TEXT NOT NULL, confidence_change REAL NOT NULL, occurred_at TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS success_patterns (id INTEGER PRIMARY KEY, pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, pattern_data TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS antipatterns (id INTEGER PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, why_problematic TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT, terminal TEXT NOT NULL, track TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS pattern_usage (pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
    """)
    con.commit()
    con.close()

    rc = state_dir / "runtime_coordination.db"
    con = sqlite3.connect(rc)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (id INTEGER PRIMARY KEY, attempt_id TEXT, dispatch_id TEXT, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT UNIQUE, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS terminal_leases (id INTEGER PRIMARY KEY, terminal_id TEXT, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY, event_id TEXT, entity_type TEXT, entity_id TEXT, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
        CREATE TABLE IF NOT EXISTS intelligence_injections (id INTEGER PRIMARY KEY, injection_id TEXT, dispatch_id TEXT, project_id TEXT NOT NULL DEFAULT 'sales-copilot');
    """)
    con.commit()
    con.close()

    dt = state_dir / "dispatch_tracker.db"
    con = sqlite3.connect(dt)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_experiments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id         TEXT UNIQUE,
            project_id          TEXT NOT NULL DEFAULT 'sales-copilot',
            timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
            instruction_chars   INTEGER,
            context_items       INTEGER,
            repo_map_symbols    INTEGER,
            role                TEXT,
            cognition           TEXT,
            model               TEXT,
            terminal            TEXT,
            file_count          INTEGER,
            success             BOOLEAN,
            cqs                 REAL,
            completion_minutes  REAL,
            test_count          INTEGER,
            committed           BOOLEAN,
            lines_changed       INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_de_dispatch_id  ON dispatch_experiments (dispatch_id);
        CREATE INDEX IF NOT EXISTS idx_de_role         ON dispatch_experiments (role);
        CREATE INDEX IF NOT EXISTS idx_de_timestamp    ON dispatch_experiments (timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_de_project_id   ON dispatch_experiments (project_id);
    """)
    con.commit()
    con.close()

    result = apply_prep("sales-copilot", state_dir, dry_run=False)

    assert result.errors == []
    alter_applied = [
        r for r in result.details
        if r.action == "applied" and r.stmt_type == "alter_table"
    ]
    assert alter_applied == [], f"Expected 0 ALTERs on fully-prepped DB, got: {alter_applied}"


def test_apply_prep_unknown_project_returns_error(tmp_path: Path):
    """Unknown project_id returns error list, does not raise."""
    result = apply_prep("unknown-project", tmp_path, dry_run=True)
    assert result.errors != []
    assert "unknown-project" in result.errors[0]


def test_apply_prep_missing_dir_returns_error():
    """Non-existent source_db_dir returns error from CLI main()."""
    from scripts.apply_schema_prep import main
    rc = main(["--project-id", "sales-copilot", "--source-db-dir", "/nonexistent/path", "--dry-run"])
    assert rc == 2
