#!/usr/bin/env python3
"""VNX Migration 0014 — report_findings table runner.

Idempotently creates the ``report_findings`` table and its indexes in
``quality_intelligence.db``. Reapplying is a no-op because both the CREATE
TABLE and CREATE INDEX statements use IF NOT EXISTS.

Source of truth for the SQL: ``schemas/migrations/0014_add_report_findings.sql``

Called by ``link_sessions_dispatches.py`` before its first SELECT on
``report_findings`` so Phase 3 of the nightly pipeline is self-healing even
when Phase 0 (quality_db_init.py) failed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATION_SQL = """
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


def ensure_report_findings_table(conn: sqlite3.Connection) -> bool:
    """Create report_findings table + indexes if they do not exist. Idempotent.

    Returns True if the table was freshly created, False if it already existed.
    """
    existed = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
        ).fetchone()
        is not None
    )
    conn.executescript(_MIGRATION_SQL)
    conn.commit()
    return not existed
