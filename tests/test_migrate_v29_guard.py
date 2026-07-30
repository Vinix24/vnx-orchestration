#!/usr/bin/env python3
"""Tests for migration V29's idempotency guard and rebuilt-table default.

Covers two defects found on PR #1248's original _migrate_v29 (session_analytics
composite UNIQUE (project_id, session_id), ADR-007):

  1. The re-run guard compared literal SQL text against sqlite_master.sql, which
     never matches SQLite's own reformatted DDL (reversed column order, quoted
     identifiers). _has_composite_unique reads PRAGMA index_list/index_info
     instead, so it is order- and quoting-independent.
  2. The rebuilt staging table declared `project_id TEXT NOT NULL DEFAULT
     'vnx-dev'`, which survived the RENAME and left migrated tables carrying a
     default the canonical schema explicitly forbids (ADR-007: writers fail
     closed via resolve_stamp_project_id, never a silent vnx-dev fallback).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import quality_db_init as qdi  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_legacy_session_analytics_db(path: Path, row_count: int = 3) -> sqlite3.Connection:
    """Legacy V3-era shape: single-column UNIQUE(session_id), no project_id."""
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None
    conn.executescript("""
        CREATE TABLE session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL,
            terminal TEXT,
            session_date DATE NOT NULL,
            total_input_tokens INTEGER DEFAULT 0,
            session_model TEXT DEFAULT 'unknown',
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzer_version TEXT DEFAULT '1.0.0'
        );
        CREATE TABLE dispatch_metadata (
            dispatch_id TEXT,
            terminal TEXT,
            role TEXT,
            gate TEXT,
            outcome_status TEXT,
            pattern_count INTEGER,
            instruction_char_count INTEGER
        );
        CREATE VIEW cost_per_dispatch AS
            SELECT dm.dispatch_id, dm.terminal, dm.role, dm.gate, dm.outcome_status
            FROM dispatch_metadata dm;
    """)
    for i in range(row_count):
        conn.execute(
            "INSERT INTO session_analytics (session_id, project_path, session_date) "
            "VALUES (?, ?, ?)",
            (f"sess-{i:04d}", "/some/path", "2026-01-01"),
        )
    conn.commit()
    return conn


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


def _sql_trace(conn: sqlite3.Connection) -> list[str]:
    """Attach a trace callback and return the (mutable) list it appends to."""
    log: list[str] = []
    conn.set_trace_callback(lambda sql: log.append(sql))
    return log


# ---------------------------------------------------------------------------
# Finding 1 — guard must be order/quoting independent
# ---------------------------------------------------------------------------

class TestCompositeUniqueGuard:
    def test_matches_reversed_and_quoted_column_order(self, tmp_path):
        """The live-DB shape: UNIQUE ("session_id", "project_id") — reversed order,
        quoted identifiers. A literal string-match guard never matches this."""
        db = tmp_path / "reversed.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                UNIQUE ("session_id", "project_id")
            )
        """)
        assert qdi._has_composite_unique(
            conn, "session_analytics", {"project_id", "session_id"}
        ) is True
        conn.close()

    def test_rejects_single_column_unique(self, tmp_path):
        """A legacy single-column UNIQUE(session_id) must NOT be mistaken for
        the composite (project_id, session_id) constraint — the guard must
        still trigger a rebuild in this case."""
        db = tmp_path / "legacy.db"
        conn = _make_legacy_session_analytics_db(db)
        assert qdi._has_composite_unique(
            conn, "session_analytics", {"project_id", "session_id"}
        ) is False
        conn.close()


# ---------------------------------------------------------------------------
# Finding 1 — full migration idempotency
# ---------------------------------------------------------------------------

class TestMigrateV29Idempotency:
    def test_second_run_is_noop_on_legacy_fixture(self, tmp_path):
        db = tmp_path / "legacy.db"
        conn = _make_legacy_session_analytics_db(db, row_count=5)

        # First run must rebuild (legacy single-column UNIQUE).
        trace = _sql_trace(conn)
        qdi._migrate_v29(conn)
        conn.commit()
        assert any("CREATE TABLE _session_analytics_v29" in s for s in trace)

        row_count_after_first = conn.execute(
            "SELECT COUNT(*) FROM session_analytics"
        ).fetchone()[0]
        assert row_count_after_first == 5

        # Second run on the now-composite-UNIQUE table must be a no-op.
        trace.clear()
        qdi._migrate_v29(conn)
        conn.commit()
        assert not any("CREATE TABLE _session_analytics_v29" in s for s in trace)
        assert not any("DROP TABLE session_analytics" in s for s in trace)

        row_count_after_second = conn.execute(
            "SELECT COUNT(*) FROM session_analytics"
        ).fetchone()[0]
        assert row_count_after_second == 5

        conn.close()

    def test_noop_when_already_migrated_with_reversed_column_order(self, tmp_path):
        """Simulates the real live-DB shape post-migration: composite UNIQUE
        already present but recorded in reversed/quoted form. Must not rebuild."""
        db = tmp_path / "already_migrated.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_path TEXT NOT NULL,
                session_date DATE NOT NULL,
                UNIQUE ("session_id", "project_id")
            )
        """)
        conn.commit()

        trace = _sql_trace(conn)
        qdi._migrate_v29(conn)
        conn.commit()
        assert not any("CREATE TABLE _session_analytics_v29" in s for s in trace)
        assert not any("DROP TABLE session_analytics" in s for s in trace)
        conn.close()


# ---------------------------------------------------------------------------
# Finding 2 — rebuilt table must carry NO default on project_id
# ---------------------------------------------------------------------------

class TestNoProjectIdDefaultAfterRebuild:
    def test_rebuilt_table_has_no_project_id_default(self, tmp_path):
        db = tmp_path / "legacy.db"
        conn = _make_legacy_session_analytics_db(db, row_count=2)

        qdi._migrate_v29(conn)
        conn.commit()

        sql = _table_sql(conn, "session_analytics")
        assert "DEFAULT 'vnx-dev'" not in sql
        assert "UNIQUE (project_id, session_id)" in sql

        col_info = {
            r[1]: r[4]  # name -> dflt_value
            for r in conn.execute("PRAGMA table_info(session_analytics)").fetchall()
        }
        assert col_info["project_id"] is None
        conn.close()
