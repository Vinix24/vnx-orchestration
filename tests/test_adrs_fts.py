"""tests/test_adrs_fts.py — FTS5 search over adrs table (PR-INT-1).

Verifies:
- FTS5 query 'multi-tenant' returns ADR-007
- FTS5 query on title finds correct ADR
- FTS5 sync triggers work: insert-then-query returns results
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
from quality_db_init import _migrate_v19


_ADR_007_SUMMARY = (
    "All multi-tenant tables in central VNX state DBs carry a project_id TEXT NOT NULL "
    "DEFAULT 'vnx-dev' column. Every UNIQUE constraint on a natural key is composite "
    "over project_id. The importer prefix-rewrites with <project_id>:<source_id> for "
    "shared identifier columns."
)

_ADR_001_SUMMARY = (
    "VNX must not use external Redis or any external key-value store for runtime state. "
    "All coordination state must be stored in local SQLite databases under .vnx-data/."
)


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "qi.db"))
    conn.isolation_level = None
    schema_migration.apply_if_below(conn, 19, _migrate_v19)
    return conn


def _insert_adr(conn: sqlite3.Connection, adr_id: str, title: str, summary: str) -> None:
    conn.execute(
        """
        INSERT INTO adrs
            (adr_id, project_id, status, title, decision_summary, file_path, source_hash)
        VALUES (?, 'vnx-dev', 'Accepted', ?, ?, ?, ?)
        """,
        (adr_id, title, summary, f"docs/{adr_id}.md", adr_id[:8]),
    )


class TestAdrsFts:
    def test_fts5_query_multitenant_returns_adr007(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_adr(conn, "ADR-007", "Multi-tenant project_id Stamping Pattern", _ADR_007_SUMMARY)
        _insert_adr(conn, "ADR-001", "No external Redis", _ADR_001_SUMMARY)

        # FTS5 requires quoting for hyphenated terms to avoid subtraction parse
        rows = conn.execute(
            'SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH \'"multi-tenant"\''
        ).fetchall()
        adr_ids = {r[0] for r in rows}
        assert "ADR-007" in adr_ids, f"ADR-007 not in FTS results: {adr_ids}"

    def test_fts5_query_redis_returns_adr001(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_adr(conn, "ADR-007", "Multi-tenant project_id Stamping Pattern", _ADR_007_SUMMARY)
        _insert_adr(conn, "ADR-001", "No external Redis", _ADR_001_SUMMARY)

        rows = conn.execute(
            "SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH 'Redis'"
        ).fetchall()
        adr_ids = {r[0] for r in rows}
        assert "ADR-001" in adr_ids

    def test_fts5_query_project_id_returns_adr007(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_adr(conn, "ADR-007", "Multi-tenant project_id Stamping Pattern", _ADR_007_SUMMARY)
        _insert_adr(conn, "ADR-001", "No external Redis", _ADR_001_SUMMARY)

        rows = conn.execute(
            "SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH 'project_id'"
        ).fetchall()
        adr_ids = {r[0] for r in rows}
        assert "ADR-007" in adr_ids

    def test_fts5_empty_on_nonexistent_term(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_adr(conn, "ADR-007", "Multi-tenant project_id Stamping Pattern", _ADR_007_SUMMARY)

        rows = conn.execute(
            "SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH 'xyznonexistenttermxyz'"
        ).fetchall()
        assert rows == []

    def test_fts5_updated_after_delete(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_adr(conn, "ADR-007", "Multi-tenant project_id Stamping Pattern", _ADR_007_SUMMARY)

        # Verify it's in FTS before delete
        rows = conn.execute(
            'SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH \'"multi-tenant"\''
        ).fetchall()
        assert len(rows) >= 1

        # Delete from base table — trigger should remove from FTS
        conn.execute("DELETE FROM adrs WHERE adr_id='ADR-007' AND project_id='vnx-dev'")

        rows_after = conn.execute(
            'SELECT adr_id FROM adrs_fts WHERE adrs_fts MATCH \'"multi-tenant"\''
        ).fetchall()
        assert rows_after == []
