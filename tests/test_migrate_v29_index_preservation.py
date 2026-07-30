#!/usr/bin/env python3
"""Tests for the codex round-4 finding on migration V29 (PR #1248, FINAL: FAIL Q4).

_migrate_v29's session_analytics rebuild (scripts/quality_db_init.py) dropped
the original table and recreated a hardcoded list of 6 indexes, silently
losing any index outside that list. Measured against the live
~/.vnx-data/vnx-dev/state/quality_intelligence.db, three were lost:

  - ux_session_analytics_pid — the ADR-007 UNIQUE (project_id, id) constraint
    from _migrate_v27. Losing this is a governance-constraint regression, not
    a performance one.
  - idx_session_analytics_project on (project_id), from
    schemas/migrations/0010_add_project_id.sql.
  - idx_session_dispatch_id on (dispatch_id) — replaced by a DIFFERENTLY named
    idx_session_dispatch, so the rebuild also left two conventions floating.

Covers:
  1. Every real (non-autoindex) index the live table has survives the rebuild,
     captured from sqlite_master rather than a hardcoded list — including an
     index no migration in this file has ever heard of.
  2. The dispatch_id index converges on exactly one canonical name
     (idx_session_dispatch, matching the current base schema) regardless of
     which name the live table carried before the rebuild.
  3. A column present in the live table but absent from the v29 staging DDL
     raises instead of being silently dropped.
  4. A row-count mismatch after the INSERT OR IGNORE copy raises instead of
     silently proceeding with fewer rows than the source had.
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

def _make_live_shaped_db(path: Path, row_count: int = 5) -> sqlite3.Connection:
    """Reproduce the real live-DB shape measured on vnx-dev at user_version=28:
    9 indexes total (8 real + 1 sqlite_autoindex from the single-column UNIQUE
    on session_id), project_id already present as a plain column (added by an
    earlier ALTER, not yet part of a composite UNIQUE).
    """
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None
    conn.executescript("""
        CREATE TABLE session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            project_path TEXT NOT NULL,
            terminal TEXT,
            session_date DATE NOT NULL,
            total_input_tokens INTEGER DEFAULT 0,
            session_model TEXT DEFAULT 'unknown',
            dispatch_id TEXT,
            primary_activity TEXT,
            context_reset_count INTEGER DEFAULT 0,
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzer_version TEXT DEFAULT '1.0.0'
        );
        CREATE INDEX idx_session_terminal ON session_analytics (terminal, session_date DESC);
        CREATE INDEX idx_session_project ON session_analytics (project_path, session_date DESC);
        CREATE INDEX idx_session_date ON session_analytics (session_date DESC);
        CREATE INDEX idx_session_activity ON session_analytics (primary_activity);
        CREATE INDEX idx_session_model ON session_analytics (session_model, session_date DESC);
        CREATE INDEX idx_session_dispatch_id ON session_analytics (dispatch_id);
        CREATE INDEX idx_session_analytics_project ON session_analytics (project_id);
        CREATE UNIQUE INDEX ux_session_analytics_pid ON session_analytics (project_id, id);

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
            "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
            "VALUES (?, ?, ?, ?)",
            (f"sess-{i:04d}", "vnx-dev", "/some/path", "2026-01-01"),
        )
    conn.commit()
    return conn


def _index_signatures(conn: sqlite3.Connection, table: str) -> dict[str, tuple[bool, frozenset]]:
    """Map index name -> (is_unique, frozenset(columns)) for every real index."""
    out = {}
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name, is_unique = idx[1], bool(idx[2])
        if name.startswith("sqlite_autoindex_"):
            continue
        cols = frozenset(
            r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()
        )
        out[name] = (is_unique, cols)
    return out


# ---------------------------------------------------------------------------
# 1 + 2: index preservation + canonical dispatch_id naming
# ---------------------------------------------------------------------------

class TestIndexPreservation:
    def test_all_live_indexes_survive_rebuild(self, tmp_path):
        db = tmp_path / "live_shaped.db"
        conn = _make_live_shaped_db(db, row_count=7)

        before = _index_signatures(conn, "session_analytics")
        assert set(before) == {
            "idx_session_terminal", "idx_session_project", "idx_session_date",
            "idx_session_activity", "idx_session_model", "idx_session_dispatch_id",
            "idx_session_analytics_project", "ux_session_analytics_pid",
        }

        qdi._migrate_v29(conn)
        conn.commit()

        after = _index_signatures(conn, "session_analytics")

        # The ADR-007 composite UNIQUE from _migrate_v27 must survive the
        # rebuild — this was the blocking finding (governance constraint, not
        # a performance regression).
        assert "ux_session_analytics_pid" in after
        assert after["ux_session_analytics_pid"] == (True, frozenset({"project_id", "id"}))

        # idx_session_analytics_project (from schemas/migrations/0010) must
        # also survive — it was not on the hardcoded recreate list either.
        assert "idx_session_analytics_project" in after
        assert after["idx_session_analytics_project"] == (False, frozenset({"project_id"}))

        # Every other pre-existing real index survives under its own name.
        for name in ("idx_session_terminal", "idx_session_project", "idx_session_date",
                     "idx_session_activity", "idx_session_model"):
            assert name in after, f"{name} was dropped by the rebuild"
            assert before[name] == after[name]

        # dispatch_id: exactly one canonical index survives (idx_session_dispatch,
        # matching the current base schema) — the legacy name is gone, not
        # floating alongside it.
        assert "idx_session_dispatch" in after
        assert after["idx_session_dispatch"] == (False, frozenset({"dispatch_id"}))
        assert "idx_session_dispatch_id" not in after

        # No index count drift: 7 preserved-by-name + 1 renamed (dispatch_id) = 8.
        assert len(after) == 8

        assert conn.execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0] == 7
        conn.close()

    def test_unknown_index_outside_any_hardcoded_list_survives(self, tmp_path):
        """Structural proof: an index this migration file has never named
        anywhere still survives, because it is captured from sqlite_master
        rather than looked up in a list."""
        db = tmp_path / "unknown_index.db"
        conn = _make_live_shaped_db(db, row_count=2)
        conn.execute(
            "CREATE INDEX idx_totally_unrelated_marker "
            "ON session_analytics (analyzer_version)"
        )
        conn.commit()

        qdi._migrate_v29(conn)
        conn.commit()

        after = _index_signatures(conn, "session_analytics")
        assert "idx_totally_unrelated_marker" in after
        assert after["idx_totally_unrelated_marker"] == (False, frozenset({"analyzer_version"}))
        conn.close()

    def test_second_run_after_index_preserving_rebuild_is_noop(self, tmp_path):
        db = tmp_path / "live_shaped.db"
        conn = _make_live_shaped_db(db, row_count=3)

        qdi._migrate_v29(conn)
        conn.commit()
        after_first = _index_signatures(conn, "session_analytics")

        # Re-running directly (bypassing the user_version gate) must not error
        # and must not change the index set — _has_composite_unique already
        # sees the correct shape, so needs_rebuild is False.
        qdi._migrate_v29(conn)
        conn.commit()
        after_second = _index_signatures(conn, "session_analytics")

        assert after_first == after_second
        assert conn.execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0] == 3
        conn.close()


# ---------------------------------------------------------------------------
# 3: source-only-column guard
# ---------------------------------------------------------------------------

class TestSourceOnlyColumnGuard:
    def test_raises_when_live_table_has_column_staging_ddl_lacks(self, tmp_path):
        db = tmp_path / "extra_column.db"
        conn = _make_live_shaped_db(db, row_count=2)
        conn.execute(
            "ALTER TABLE session_analytics ADD COLUMN totally_unknown_column TEXT"
        )
        conn.execute(
            "UPDATE session_analytics SET totally_unknown_column = 'must-not-be-dropped'"
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="totally_unknown_column"):
            qdi._migrate_v29(conn)

        # The guard must fire before the destructive DROP TABLE step — the
        # original table (and its data) must still be intact.
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_analytics'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM session_analytics"
        ).fetchone()[0] == 2
        conn.close()


# ---------------------------------------------------------------------------
# 4: copy-count assertion
# ---------------------------------------------------------------------------

class TestCopyCountAssertion:
    def test_raises_on_row_count_mismatch_after_copy(self, tmp_path):
        """A pre-composite-UNIQUE table with no per-column uniqueness at all
        (an even-older legacy shape) can carry two rows that collide on
        (project_id, session_id). INSERT OR IGNORE silently drops one on
        copy — the migration must refuse to proceed rather than swap in a
        table with fewer rows than the source had.
        """
        db = tmp_path / "duplicate_rows.db"
        conn = sqlite3.connect(str(db))
        conn.isolation_level = None
        conn.executescript("""
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                project_path TEXT NOT NULL,
                session_date DATE NOT NULL
            );
            CREATE TABLE dispatch_metadata (
                dispatch_id TEXT, terminal TEXT, role TEXT, gate TEXT,
                outcome_status TEXT, pattern_count INTEGER, instruction_char_count INTEGER
            );
            CREATE VIEW cost_per_dispatch AS
                SELECT dm.dispatch_id, dm.terminal, dm.role, dm.gate, dm.outcome_status
                FROM dispatch_metadata dm;
        """)
        # Two rows that collide on (project_id, session_id) — legal here
        # because this legacy shape has no UNIQUE constraint at all.
        conn.execute(
            "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
            "VALUES ('dup-session', 'vnx-dev', '/p', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
            "VALUES ('dup-session', 'vnx-dev', '/p', '2026-01-01')"
        )
        conn.commit()

        source_count_before = conn.execute(
            "SELECT COUNT(*) FROM session_analytics"
        ).fetchone()[0]
        assert source_count_before == 2

        with pytest.raises(RuntimeError, match="row-count mismatch"):
            qdi._migrate_v29(conn)

        # Must not have swapped in the lossy table — original still intact.
        assert conn.execute(
            "SELECT COUNT(*) FROM session_analytics"
        ).fetchone()[0] == 2
        conn.close()
