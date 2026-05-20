"""
Unit tests for Wave 8 HIGH-severity blockers.

BLOCKER 2: _query_candidates passes project_id_fn → multi-tenant isolation in central DB.
BLOCKER 3: _table_has_column rejects unknown table names via VALID_TABLES allowlist.
"""
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/lib is on the path so imports resolve without package install.
SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# BLOCKER 3: _table_has_column allowlist
# ---------------------------------------------------------------------------

from intelligence_sources._common import VALID_TABLES, _table_has_column


def _in_memory_conn_with_table(table: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, project_id TEXT)")
    conn.commit()
    return conn


def test_table_has_column_valid_table_column_present():
    table = "success_patterns"
    conn = _in_memory_conn_with_table(table)
    assert _table_has_column(conn, table, "project_id") is True


def test_table_has_column_valid_table_column_absent():
    table = "antipatterns"
    conn = _in_memory_conn_with_table(table)
    assert _table_has_column(conn, table, "nonexistent_col") is False


def test_table_has_column_rejects_sql_injection():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="Unknown table"):
        _table_has_column(conn, "users; DROP TABLE x;--", "col")


def test_table_has_column_rejects_path_traversal():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="Unknown table"):
        _table_has_column(conn, "../../etc/passwd", "col")


def test_table_has_column_rejects_empty_string():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="Unknown table"):
        _table_has_column(conn, "", "col")


def test_table_has_column_all_valid_tables_accepted():
    """Every table in VALID_TABLES must pass the guard (no ValueError)."""
    conn = sqlite3.connect(":memory:")
    for table in VALID_TABLES:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
        conn.commit()
        # Should return False (column missing), not raise.
        result = _table_has_column(conn, table, "nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# BLOCKER 2: project_id_fn propagation in _query_candidates
# ---------------------------------------------------------------------------

from intelligence_selector import IntelligenceSelector


def _make_selector_with_mock_db(db: sqlite3.Connection) -> IntelligenceSelector:
    sel = IntelligenceSelector.__new__(IntelligenceSelector)
    sel._quality_db_path = None
    sel._coord_state_dir = None
    sel._quality_db = db
    return sel


def _central_conn_that_filters(project_id: str) -> sqlite3.Connection:
    """Return an in-memory DB with one success_patterns row scoped to project_id.

    Schema mirrors what _query_central in proven_pattern.py expects:
    id, title, description, category, confidence_score, usage_count,
    source_dispatch_ids, first_seen, last_used, valid_until,
    pattern_category, content_hash, project_id.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            description TEXT,
            category TEXT,
            confidence_score REAL,
            usage_count INTEGER,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_used TEXT,
            valid_until TEXT,
            pattern_category TEXT,
            content_hash TEXT,
            project_id TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO success_patterns
           (title, description, category, confidence_score, usage_count,
            source_dispatch_ids, first_seen, last_used, valid_until,
            pattern_category, content_hash, project_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Use atomic writes",
            "Always write to .tmp then os.replace for atomic file updates.",
            "",  # empty category = matches all scope_tags
            0.9, 3, "[]",
            "2026-01-01T00:00:00Z", "2026-05-01T00:00:00Z",
            None,  # valid_until NULL = always valid
            "code", "abc123", project_id,
        ),
    )
    conn.commit()
    return conn


def _make_central_conn_factory(conn: sqlite3.Connection):
    """Return a callable that yields conn without closing it (for multi-call safety)."""
    calls = [0]

    def factory():
        calls[0] += 1
        if calls[0] > 1:
            # _query_central closes the connection; re-open a fresh in-memory copy
            # by returning None for subsequent calls (failure_prevention / recent_comparable).
            return None
        return conn

    return factory


def test_query_candidates_passes_project_id_fn():
    """
    With VNX_USE_CENTRAL_DB=1, project_id_fn='test-project' yields rows from central DB.
    Proves project_id_fn is forwarded from _query_candidates → query_proven_patterns
    → _query_central.
    """
    test_project = "test-project"
    central_conn = _central_conn_that_filters(test_project)
    local_db = sqlite3.connect(":memory:")
    local_db.row_factory = sqlite3.Row

    sel = _make_selector_with_mock_db(local_db)

    factory = _make_central_conn_factory(central_conn)

    with patch.dict(os.environ, {"VNX_USE_CENTRAL_DB": "1"}):
        with patch("intelligence_selector.current_project_id", return_value=test_project):
            with patch.object(sel, "_get_central_qi_conn", side_effect=factory):
                result = sel._query_candidates("backend", [])

    proven = result.get("proven_pattern", [])
    assert len(proven) > 0, (
        "Expected proven_pattern rows from central DB for matching project_id but got none. "
        "project_id_fn is likely not being passed from _query_candidates to query_proven_patterns."
    )


def test_query_candidates_isolates_other_project():
    """
    With VNX_USE_CENTRAL_DB=1 and project_id_fn='other-project', rows scoped to
    'test-project' must NOT appear — multi-tenant isolation enforced.
    """
    test_project = "test-project"
    other_project = "other-project"
    central_conn = _central_conn_that_filters(test_project)
    local_db = sqlite3.connect(":memory:")
    local_db.row_factory = sqlite3.Row

    sel = _make_selector_with_mock_db(local_db)

    factory = _make_central_conn_factory(central_conn)

    with patch.dict(os.environ, {"VNX_USE_CENTRAL_DB": "1"}):
        with patch("intelligence_selector.current_project_id", return_value=other_project):
            with patch.object(sel, "_get_central_qi_conn", side_effect=factory):
                result = sel._query_candidates("backend", [])

    proven = result.get("proven_pattern", [])
    assert len(proven) == 0, (
        f"Rows for '{test_project}' leaked into '{other_project}' context. "
        "Multi-tenant isolation via project_id_fn is broken."
    )
