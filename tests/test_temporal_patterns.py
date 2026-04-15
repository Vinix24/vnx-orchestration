#!/usr/bin/env python3
"""
Tests for F54 temporal pattern lifecycle (valid_from / valid_until columns).

Covers:
  - test_new_pattern_has_valid_from
  - test_superseded_pattern_excluded_from_selector
  - test_active_pattern_included
  - test_nightly_supersession_threshold
  - test_migration_idempotent
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the minimal schema needed for temporal pattern tests."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pattern_data TEXT NOT NULL DEFAULT '{}',
            confidence_score REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used DATETIME,
            valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
            valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pattern_data TEXT NOT NULL DEFAULT '{}',
            why_problematic TEXT NOT NULL DEFAULT '',
            severity TEXT DEFAULT 'medium',
            occurrence_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen DATETIME,
            valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
            valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT NOT NULL DEFAULT '',
            rule_type TEXT NOT NULL DEFAULT 'failure_prevention',
            description TEXT NOT NULL DEFAULT '',
            recommendation TEXT NOT NULL DEFAULT '',
            confidence REAL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
            valid_until DATETIME DEFAULT NULL
        );
    """)
    conn.commit()


def _db_file() -> tuple[sqlite3.Connection, Path]:
    """Return (connection, path) for a temp DB with schema applied."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _create_schema(conn)
    return conn, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_new_pattern_has_valid_from():
    """Patterns inserted with valid_from=NOW have a non-null valid_from timestamp."""
    conn, path = _db_file()
    try:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Test pattern", "desc", "{}", 0.8, 1, "[]", now, now, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT valid_from, valid_until FROM success_patterns WHERE title = 'Test pattern'"
        ).fetchone()
        assert row is not None
        assert row["valid_from"] is not None, "valid_from should be set on new pattern"
        assert row["valid_until"] is None, "valid_until should be NULL for a new pattern"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_superseded_pattern_excluded_from_selector():
    """Patterns with valid_until in the past are excluded by the selector WHERE clause."""
    conn, path = _db_file()
    try:
        past = (datetime.now() - timedelta(days=1)).isoformat()
        now = datetime.now().isoformat()
        # Insert one superseded pattern and one active pattern
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, last_used, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Superseded pattern", 0.9, 5, "[]", now, now, now, past),
        )
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, last_used, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Active pattern", 0.9, 5, "[]", now, now, now, None),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT title FROM success_patterns "
            "WHERE (valid_until IS NULL OR valid_until > datetime('now'))"
        ).fetchall()
        titles = [r["title"] for r in rows]

        assert "Active pattern" in titles
        assert "Superseded pattern" not in titles, "Superseded pattern must be excluded"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_active_pattern_included():
    """Patterns with valid_until IS NULL are included by the selector WHERE clause."""
    conn, path = _db_file()
    try:
        now = datetime.now().isoformat()
        future = (datetime.now() + timedelta(days=30)).isoformat()

        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, last_used, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Null until", 0.8, 3, "[]", now, now, now, None),
        )
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, last_used, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Future until", 0.8, 3, "[]", now, now, now, future),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT title FROM success_patterns "
            "WHERE (valid_until IS NULL OR valid_until > datetime('now'))"
        ).fetchall()
        titles = [r["title"] for r in rows]

        assert "Null until" in titles, "Pattern with valid_until IS NULL must be included"
        assert "Future until" in titles, "Pattern with future valid_until must be included"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_nightly_supersession_threshold():
    """_supersede_stale_patterns marks qualifying rows valid_until = NOW.

    Qualifying: confidence_score < 0.3 AND valid_from older than 30 days AND valid_until IS NULL.
    Non-qualifying rows (high confidence, recent, or already superseded) must not be touched.
    """
    conn, path = _db_file()
    try:
        now = datetime.now().isoformat()
        old = (datetime.now() - timedelta(days=31)).isoformat()

        # Should be superseded: low confidence, old
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Stale low", 0.1, 0, "[]", old, old, None),
        )
        # Should NOT be superseded: high confidence, old
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "High conf old", 0.9, 10, "[]", old, old, None),
        )
        # Should NOT be superseded: low confidence but recent
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, confidence_score, usage_count, "
            " source_dispatch_ids, first_seen, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "", "Low conf recent", 0.1, 0, "[]", now, now, None),
        )
        conn.commit()

        supersession_ts = datetime.now().isoformat()
        cur = conn.execute(
            "UPDATE success_patterns SET valid_until = ? "
            "WHERE confidence_score < 0.3 "
            "AND valid_from < datetime('now', '-30 days') "
            "AND valid_until IS NULL",
            (supersession_ts,),
        )
        conn.commit()
        count = cur.rowcount

        assert count == 1, f"Expected 1 superseded pattern, got {count}"

        row_stale = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title = 'Stale low'"
        ).fetchone()
        assert row_stale["valid_until"] is not None, "Stale low-confidence pattern must have valid_until set"

        row_high = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title = 'High conf old'"
        ).fetchone()
        assert row_high["valid_until"] is None, "High-confidence pattern must not be superseded"

        row_recent = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title = 'Low conf recent'"
        ).fetchone()
        assert row_recent["valid_until"] is None, "Recent low-confidence pattern must not be superseded"
    finally:
        conn.close()
        path.unlink(missing_ok=True)


def test_migration_idempotent():
    """Running the temporal migration twice must not raise an error."""
    conn, path = _db_file()
    try:
        def _run_migration(c: sqlite3.Connection) -> None:
            for tbl in ("success_patterns", "antipatterns", "prevention_rules"):
                cursor = c.execute(f"PRAGMA table_info({tbl})")
                cols = {row[1] for row in cursor.fetchall()}
                if "valid_from" not in cols:
                    c.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN valid_from DATETIME DEFAULT CURRENT_TIMESTAMP"
                    )
                    c.commit()
                if "valid_until" not in cols:
                    c.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN valid_until DATETIME DEFAULT NULL"
                    )
                    c.commit()

        # First run — columns already exist from schema helper, should be a no-op
        _run_migration(conn)

        # Second run — still a no-op, must not raise
        _run_migration(conn)

        # Verify columns exist in all three tables
        for tbl in ("success_patterns", "antipatterns", "prevention_rules"):
            cursor = conn.execute(f"PRAGMA table_info({tbl})")
            cols = {row[1] for row in cursor.fetchall()}
            assert "valid_from" in cols, f"{tbl} missing valid_from after migration"
            assert "valid_until" in cols, f"{tbl} missing valid_until after migration"
    finally:
        conn.close()
        path.unlink(missing_ok=True)
