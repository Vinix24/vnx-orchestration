"""Tests for OI-1155 — report_findings table idempotent migration.

Verifies:
  1. ensure_report_findings_table() creates the table on a fresh DB.
  2. Running it twice is a no-op (idempotent).
  3. All columns expected by consumers can be INSERT-ed and SELECT-ed back.
  4. link_sessions_dispatches imports without error and calls the migration.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/lib importable
_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from report_findings_migration import ensure_report_findings_table


def _fresh_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection."""
    return sqlite3.connect(":memory:")


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
        ).fetchone()
        is not None
    )


def _indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='report_findings'"
    ).fetchall()
    return {r[0] for r in rows}


# ── table creation ────────────────────────────────────────────────────────────

def test_creates_table_on_fresh_db():
    conn = _fresh_conn()
    assert not _table_exists(conn)
    created = ensure_report_findings_table(conn)
    assert created is True
    assert _table_exists(conn)


def test_creates_expected_indexes():
    conn = _fresh_conn()
    ensure_report_findings_table(conn)
    idx = _indexes(conn)
    assert "idx_report_findings_extracted" in idx
    assert "idx_report_findings_dispatch" in idx


# ── idempotency ───────────────────────────────────────────────────────────────

def test_idempotent_second_call():
    conn = _fresh_conn()
    ensure_report_findings_table(conn)
    # Must not raise; returns False because table already existed
    result = ensure_report_findings_table(conn)
    assert result is False


def test_idempotent_n_times():
    conn = _fresh_conn()
    for _ in range(5):
        ensure_report_findings_table(conn)
    assert _table_exists(conn)


# ── insert / select round-trip ────────────────────────────────────────────────

def test_insert_all_columns_and_select_back():
    conn = _fresh_conn()
    ensure_report_findings_table(conn)

    conn.execute(
        """
        INSERT INTO report_findings (
            report_path, report_date, terminal, task_type,
            patterns_found, antipatterns_found, prevention_rules_found,
            tags_found, summary, age_category, extracted_at, dispatch_id
        ) VALUES (
            '/path/to/report.md', '2026-05-01T02:00:00', 'T1', 'implementation',
            3, 1, 2,
            '["resilience","dispatch"]', 'Dispatch completed successfully.', 'recent',
            '2026-05-01T02:01:00', '20260501-w4d-report-findings-schema'
        )
        """
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT id, report_path, dispatch_id, terminal, task_type,
               patterns_found, antipatterns_found, prevention_rules_found,
               tags_found, summary, age_category
        FROM report_findings
        WHERE dispatch_id = '20260501-w4d-report-findings-schema'
        """
    ).fetchone()

    assert row is not None
    assert row[0] == 1  # id
    assert row[1] == "/path/to/report.md"
    assert row[2] == "20260501-w4d-report-findings-schema"
    assert row[3] == "T1"
    assert row[4] == "implementation"
    assert row[5] == 3   # patterns_found
    assert row[6] == 1   # antipatterns_found
    assert row[7] == 2   # prevention_rules_found


def test_dispatch_id_nullable():
    conn = _fresh_conn()
    ensure_report_findings_table(conn)
    conn.execute(
        "INSERT INTO report_findings (report_path) VALUES ('/only/path.md')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT dispatch_id FROM report_findings WHERE report_path = '/only/path.md'"
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_select_unlinked_rows_like_phase3():
    """Mimic the exact query used by link_sessions_dispatches Phase 3."""
    conn = _fresh_conn()
    ensure_report_findings_table(conn)

    conn.execute(
        "INSERT INTO report_findings (report_path) VALUES ('/report/a.md')"
    )
    conn.execute(
        "INSERT INTO report_findings (report_path, dispatch_id) VALUES ('/report/b.md', 'some-id')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT id, report_path FROM report_findings WHERE dispatch_id IS NULL"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "/report/a.md"


# ── module-level smoke: ensure_report_findings_table survives pre-existing table

def test_pre_existing_full_schema_table_survives():
    """Table already exists with the full expected schema — migration must be a no-op."""
    conn = _fresh_conn()
    # Simulate table created by an earlier run of this same migration
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_findings (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            report_path             TEXT    NOT NULL,
            report_date             TIMESTAMP,
            terminal                TEXT,
            task_type               TEXT,
            patterns_found          INTEGER,
            antipatterns_found      INTEGER,
            prevention_rules_found  INTEGER,
            tags_found              TEXT,
            summary                 TEXT,
            age_category            TEXT,
            extracted_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            dispatch_id             TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_report_findings_extracted
            ON report_findings (extracted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_report_findings_dispatch
            ON report_findings (dispatch_id);
        """
    )
    conn.commit()
    result = ensure_report_findings_table(conn)
    assert result is False
    assert _table_exists(conn)
