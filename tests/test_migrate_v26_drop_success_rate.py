#!/usr/bin/env python3
"""Tests for migration V26 — drop dead success_rate column from success_patterns.

Covers:
  - v26 up: success_rate column is dropped, idx_patterns_category recreated
  - v26 down: success_rate column is re-added, idx_patterns_category restored
  - round-trip: up then down restores the original column
  - idempotency: running up twice or down twice is safe
  - build_t0_state ORDER BY: queries work correctly without success_rate
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
import build_t0_state as bts   # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_v25_db(path: Path) -> sqlite3.Connection:
    """Create a minimal success_patterns table at v25 schema (with success_rate)."""
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            success_rate REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            confidence_score REAL DEFAULT 0.5,
            tags TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_patterns_category
            ON success_patterns (category, success_rate DESC);
        PRAGMA user_version = 25;
    """)
    conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, success_rate, confidence_score) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("approach", "testing", "Pattern A", "Desc A", 0.0, 0.9),
    )
    conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, success_rate, confidence_score) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("approach", "testing", "Pattern B", "Desc B", 0.0, 0.7),
    )
    conn.commit()
    return conn


def _col_names(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Migration up
# ---------------------------------------------------------------------------

class TestMigrateV26Up:
    def test_drops_success_rate_column(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        assert "success_rate" in _col_names(conn, "success_patterns")

        qdi._migrate_v26(conn)

        assert "success_rate" not in _col_names(conn, "success_patterns")
        conn.close()

    def test_preserves_other_columns(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)

        qdi._migrate_v26(conn)

        cols = _col_names(conn, "success_patterns")
        for expected in ("id", "pattern_type", "category", "title", "description",
                         "confidence_score", "usage_count"):
            assert expected in cols, f"column {expected!r} was unexpectedly removed"
        conn.close()

    def test_preserves_row_data(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)

        qdi._migrate_v26(conn)

        rows = conn.execute(
            "SELECT title, confidence_score FROM success_patterns ORDER BY confidence_score DESC"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "Pattern A"
        assert rows[1][0] == "Pattern B"
        conn.close()

    def test_recreates_idx_patterns_category_without_success_rate(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)

        qdi._migrate_v26(conn)

        sql = _index_sql(conn, "idx_patterns_category")
        assert sql is not None, "idx_patterns_category was not recreated"
        assert "success_rate" not in sql.lower()
        conn.close()

    def test_idempotent_when_column_already_absent(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        qdi._migrate_v26(conn)

        # Running again must not raise
        qdi._migrate_v26(conn)

        assert "success_rate" not in _col_names(conn, "success_patterns")
        conn.close()

    def test_noop_when_table_absent(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None
        conn.execute("PRAGMA user_version = 25")

        # Must not raise even when table doesn't exist
        qdi._migrate_v26(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Migration down
# ---------------------------------------------------------------------------

class TestMigrateV26Down:
    def test_readds_success_rate_column(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        qdi._migrate_v26(conn)
        assert "success_rate" not in _col_names(conn, "success_patterns")

        qdi._migrate_v26_down(conn)

        assert "success_rate" in _col_names(conn, "success_patterns")
        conn.close()

    def test_column_has_default_zero(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        qdi._migrate_v26(conn)
        qdi._migrate_v26_down(conn)

        # Existing rows must read 0.0 for success_rate
        rows = conn.execute("SELECT success_rate FROM success_patterns").fetchall()
        assert all(r[0] == 0.0 for r in rows), "restored rows should default to 0.0"
        conn.close()

    def test_restores_idx_patterns_category_with_success_rate(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        qdi._migrate_v26(conn)
        qdi._migrate_v26_down(conn)

        sql = _index_sql(conn, "idx_patterns_category")
        assert sql is not None
        assert "success_rate" in sql.lower()
        conn.close()

    def test_idempotent_when_column_already_present(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)
        # column is still present (v26 not applied yet)
        assert "success_rate" in _col_names(conn, "success_patterns")

        # Running down on a DB that still has the column must not raise
        qdi._migrate_v26_down(conn)

        assert "success_rate" in _col_names(conn, "success_patterns")
        conn.close()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

class TestMigrateV26RoundTrip:
    def test_up_down_restores_column(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)

        qdi._migrate_v26(conn)
        assert "success_rate" not in _col_names(conn, "success_patterns")

        qdi._migrate_v26_down(conn)
        assert "success_rate" in _col_names(conn, "success_patterns")
        conn.close()

    def test_up_down_up_converges(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = tmp_path / "qi.db"
        conn = _make_v25_db(db_path)

        qdi._migrate_v26(conn)
        qdi._migrate_v26_down(conn)
        qdi._migrate_v26(conn)

        assert "success_rate" not in _col_names(conn, "success_patterns")
        conn.close()


# ---------------------------------------------------------------------------
# bootstrap_qi_db applies v26 automatically
# ---------------------------------------------------------------------------

class TestBootstrapAppliesV26:
    def test_bootstrap_stamps_v26(self, tmp_path: pytest.TempPathFactory) -> None:
        """A v25 DB bootstrapped with the current schema_file reaches user_version=26."""
        db_path = tmp_path / "qi.db"
        # Seed a minimal DB at v25
        conn = _make_v25_db(db_path)
        conn.close()

        from schema_migration import get_user_version, apply_if_below

        conn2 = sqlite3.connect(str(db_path))
        conn2.isolation_level = None
        apply_if_below(conn2, 26, qdi._migrate_v26)
        v = get_user_version(conn2)
        conn2.close()

        assert v == 26
        # Verify column is gone
        conn3 = sqlite3.connect(str(db_path))
        assert "success_rate" not in _col_names(conn3, "success_patterns")
        conn3.close()


# ---------------------------------------------------------------------------
# build_t0_state ORDER BY without success_rate
# ---------------------------------------------------------------------------

class TestBuildT0StateOrderBy:
    def test_intelligence_brief_sql_excludes_success_rate(self) -> None:
        assert "success_rate" not in bts._INTELLIGENCE_BRIEF_SQL.lower()
        assert "success_rate" not in bts._INTELLIGENCE_BRIEF_CENTRAL_SQL.lower()

    def test_order_by_is_confidence_score_desc(self) -> None:
        assert "order by confidence_score desc" in bts._INTELLIGENCE_BRIEF_SQL.lower()
        assert "order by confidence_score desc" in bts._INTELLIGENCE_BRIEF_CENTRAL_SQL.lower()

    def test_query_works_on_migrated_db(self, tmp_path: pytest.TempPathFactory) -> None:
        """_collect_intelligence_brief_per_project returns results after v26 migration."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = _make_v25_db(db_path)
        qdi._migrate_v26(conn)
        conn.close()

        result = bts._collect_intelligence_brief_per_project("test-pid", tmp_path)

        assert len(result) == 2
        # Ordered by confidence_score DESC: Pattern A (0.9) before Pattern B (0.7)
        assert result[0]["title"] == "Pattern A"
        assert result[1]["title"] == "Pattern B"
        assert "success_rate" not in result[0]

    def test_query_works_on_legacy_db(self, tmp_path: pytest.TempPathFactory) -> None:
        """_collect_intelligence_brief_per_project returns results from a pre-v26 DB."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = _make_v25_db(db_path)
        conn.close()

        result = bts._collect_intelligence_brief_per_project("test-pid", tmp_path)

        assert len(result) == 2
        assert result[0]["title"] == "Pattern A"
        # success_rate no longer selected even on legacy DB
        assert "success_rate" not in result[0]
