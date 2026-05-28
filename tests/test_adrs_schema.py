"""tests/test_adrs_schema.py — adrs table + FTS5 + index schema validation (PR-INT-1).

Verifies:
- V19 migration creates adrs table, FTS5 virtual table, and status index
- Composite PK (adr_id, project_id) is enforced
- FTS5 sync triggers exist
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


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "test_qi.db"))
    conn.isolation_level = None
    return conn


def _apply_v19(conn: sqlite3.Connection) -> None:
    from quality_db_init import _migrate_v19
    schema_migration.apply_if_below(conn, 19, _migrate_v19)


class TestAdrsTable:
    def test_adrs_table_created(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='adrs'"
        ).fetchone()
        assert row is not None, "adrs table not created"

    def test_adrs_fts5_created(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='adrs_fts'"
        ).fetchone()
        assert row is not None, "adrs_fts virtual table not created"

    def test_status_index_created(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_adrs_status'"
        ).fetchone()
        assert row is not None, "idx_adrs_status index not created"

    def test_triggers_created(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='adrs'"
            ).fetchall()
        }
        assert "adrs_ai" in triggers, "INSERT trigger missing"
        assert "adrs_ad" in triggers, "DELETE trigger missing"
        assert "adrs_au" in triggers, "UPDATE trigger missing"

    def test_composite_pk_enforced(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        conn.execute("""
            INSERT INTO adrs
                (adr_id, project_id, status, title, decision_summary, file_path, source_hash)
            VALUES ('ADR-001', 'vnx-dev', 'Accepted', 'Title A', 'Decision A', 'docs/a.md', 'abc')
        """)
        # Same adr_id + project_id → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""
                INSERT INTO adrs
                    (adr_id, project_id, status, title, decision_summary, file_path, source_hash)
                VALUES ('ADR-001', 'vnx-dev', 'Accepted', 'Title A dup', 'Decision dup', 'docs/a.md', 'xyz')
            """)

    def test_different_project_id_allowed(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        conn.execute("""
            INSERT INTO adrs
                (adr_id, project_id, status, title, decision_summary, file_path, source_hash)
            VALUES ('ADR-001', 'vnx-dev', 'Accepted', 'Title A', 'Decision A', 'docs/a.md', 'abc')
        """)
        # Same adr_id but different project_id → allowed
        conn.execute("""
            INSERT INTO adrs
                (adr_id, project_id, status, title, decision_summary, file_path, source_hash)
            VALUES ('ADR-001', 'other-project', 'Accepted', 'Title A', 'Decision A', 'docs/a.md', 'abc')
        """)
        count = conn.execute("SELECT COUNT(*) FROM adrs WHERE adr_id='ADR-001'").fetchone()[0]
        assert count == 2

    def test_migration_idempotent(self, tmp_path):
        conn = _make_conn(tmp_path)
        _apply_v19(conn)
        # Running again must not raise
        _apply_v19(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='adrs'"
        ).fetchone()
        assert row is not None
