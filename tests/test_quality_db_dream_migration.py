"""Regression tests for v20 dream migration (ADR-019, ADR-007).

Verifies that bootstrap_qi_db creates dream_cycles and dream_pattern_archives.
Covers: fresh bootstrap, idempotent re-run, composite PK structure (ADR-007).
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import quality_db_init
import schema_migration

_SCHEMA_FILE = REPO_ROOT / "schemas" / "quality_intelligence.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap(db_path: Path) -> bool:
    return quality_db_init.bootstrap_qi_db(db_path, _SCHEMA_FILE)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDreamMigrationV20:
    def test_bootstrap_creates_dream_cycles(self, tmp_path):
        """bootstrap_qi_db creates dream_cycles table on fresh DB."""
        db_path = tmp_path / "quality_intelligence.db"
        result = _bootstrap(db_path)
        assert result is True

        conn = sqlite3.connect(str(db_path))
        tables = _table_names(conn)
        conn.close()

        assert "dream_cycles" in tables

    def test_bootstrap_creates_dream_pattern_archives(self, tmp_path):
        """bootstrap_qi_db creates dream_pattern_archives table on fresh DB."""
        db_path = tmp_path / "quality_intelligence.db"
        result = _bootstrap(db_path)
        assert result is True

        conn = sqlite3.connect(str(db_path))
        tables = _table_names(conn)
        conn.close()

        assert "dream_pattern_archives" in tables

    def test_dream_cycles_composite_pk_adr007(self, tmp_path):
        """dream_cycles has composite PRIMARY KEY (cycle_id, project_id) per ADR-007."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        pk_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(dream_cycles)").fetchall()
            if r[5] > 0  # pk column (column 5 = pk index, 0 = not PK)
        }
        conn.close()

        assert "cycle_id" in pk_cols
        assert "project_id" in pk_cols

    def test_dream_cycles_indexes_created(self, tmp_path):
        """bootstrap creates idx_dream_cycles_project_status index."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        indexes = _index_names(conn)
        conn.close()

        assert "idx_dream_cycles_project_status" in indexes

    def test_dream_archives_indexes_created(self, tmp_path):
        """bootstrap creates idx_dream_archives_cycle index."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        indexes = _index_names(conn)
        conn.close()

        assert "idx_dream_archives_cycle" in indexes

    def test_user_version_is_highest_after_bootstrap(self, tmp_path):
        """PRAGMA user_version equals HIGHEST_QI_VERSION (21 after the provider migration)."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        version = schema_migration.get_user_version(conn)
        conn.close()

        assert version == quality_db_init.HIGHEST_QI_VERSION
        assert version == 22

    def test_bootstrap_idempotent_on_existing_db(self, tmp_path):
        """Running bootstrap twice does not corrupt dream tables or raise."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        # Insert a row so we can verify data survives the second bootstrap
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dream_cycles(cycle_id, project_id, status, provider) "
            "VALUES ('test-cycle-01', 'vnx-dev', 'completed', 'kimi')"
        )
        conn.commit()
        conn.close()

        result = _bootstrap(db_path)
        assert result is True

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT cycle_id FROM dream_cycles").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "test-cycle-01"

    def test_dream_pattern_archives_sqlite_sequence_initialized(self, tmp_path):
        """sqlite_sequence has an entry for dream_pattern_archives after bootstrap."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'dream_pattern_archives'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 0

    def test_dream_cycles_insert_and_query(self, tmp_path):
        """dream_cycles accepts INSERT and enforces project_id scoping."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dream_cycles(cycle_id, project_id, status, provider) "
            "VALUES (?, ?, ?, ?)",
            ("cycle-001", "proj-a", "completed", "kimi"),
        )
        conn.execute(
            "INSERT INTO dream_cycles(cycle_id, project_id, status, provider) "
            "VALUES (?, ?, ?, ?)",
            ("cycle-001", "proj-b", "completed", "kimi"),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT cycle_id, project_id FROM dream_cycles ORDER BY project_id"
        ).fetchall()
        conn.close()

        assert len(rows) == 2
        assert rows[0] == ("cycle-001", "proj-a")
        assert rows[1] == ("cycle-001", "proj-b")

    def test_dream_migration_does_not_drop_existing_tables(self, tmp_path):
        """v20 migration does not drop or truncate tables that already exist."""
        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dream_cycles(cycle_id, project_id, status, provider) "
            "VALUES ('keep-me', 'vnx-dev', 'completed', 'kimi')"
        )
        conn.commit()

        # Manually downgrade user_version to simulate re-running v20
        conn.execute("PRAGMA user_version = 19")
        conn.commit()
        conn.close()

        result = _bootstrap(db_path)
        assert result is True

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT cycle_id FROM dream_cycles WHERE cycle_id = 'keep-me'"
        ).fetchone()
        conn.close()

        assert row is not None, "existing dream_cycles row must survive re-migration"
