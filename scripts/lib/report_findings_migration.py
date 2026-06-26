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

# Canonical non-PK columns, in CREATE TABLE order, with an ALTER-safe type.
# Kept in sync with the CREATE TABLE above. Used to self-heal a drifted
# pre-existing table before the indexes are (re)created. `extracted_at` is
# added WITHOUT its CURRENT_TIMESTAMP default because SQLite's ALTER TABLE
# ADD COLUMN rejects non-constant defaults; rows created through the normal
# CREATE TABLE path still get the default.
_EXPECTED_COLUMNS: list[tuple[str, str]] = [
    ("report_path", "TEXT"),
    ("report_date", "TIMESTAMP"),
    ("terminal", "TEXT"),
    ("task_type", "TEXT"),
    ("patterns_found", "INTEGER"),
    ("antipatterns_found", "INTEGER"),
    ("prevention_rules_found", "INTEGER"),
    ("tags_found", "TEXT"),
    ("summary", "TEXT"),
    ("age_category", "TEXT"),
    ("extracted_at", "TIMESTAMP"),
    ("dispatch_id", "TEXT"),
]


def _self_heal_drifted_columns(conn: sqlite3.Connection) -> None:
    """Add any canonical columns missing from a pre-existing report_findings table.

    A table left behind by an older schema can lack columns the current
    indexes reference (notably ``extracted_at``). Without this, the
    ``CREATE INDEX ... (extracted_at DESC)`` below raises
    ``OperationalError: no such column: extracted_at`` and the whole
    self-healing migration crashes. Columns are added nullable (additive,
    no rebuild, no data loss).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(report_findings)")}
    for name, col_type in _EXPECTED_COLUMNS:
        if name not in existing:
            conn.execute(
                f"ALTER TABLE report_findings ADD COLUMN {name} {col_type}"
            )


def ensure_report_findings_table(conn: sqlite3.Connection) -> bool:
    """Create report_findings table + indexes if they do not exist. Idempotent.

    If the table already exists but has drifted (missing columns the indexes
    reference), the missing columns are added before the indexes are created,
    so a drifted-but-populated table self-heals instead of crashing.

    Returns True if the table was freshly created, False if it already existed.
    """
    existed = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
        ).fetchone()
        is not None
    )
    if existed:
        _self_heal_drifted_columns(conn)
    conn.executescript(_MIGRATION_SQL)
    conn.commit()
    return not existed
