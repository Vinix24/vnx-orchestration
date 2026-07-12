#!/usr/bin/env python3
"""Regression: pattern_usage / dispatch_pattern_offered UPSERT conflict-target mismatch.

Before this fix, the has_project branches of ``_upsert_pattern_usage`` and
``_upsert_dispatch_pattern_offered`` targeted ``ON CONFLICT(pattern_id)`` and
``ON CONFLICT(dispatch_id, pattern_id)`` respectively — column sets that do
not match the composite PRIMARY KEY / UNIQUE INDEX on the ADR-007
project-stamped tables (the PK there includes ``project_id``). SQLite raised
"ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint",
which was caught as ``sqlite3.Error`` and swallowed into a warning. First-time
inserts of a never-seen pattern worked; any re-offer of a previously-seen
pattern silently failed, so usage counts never accumulated.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_LIB = TESTS_DIR.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from intelligence_selector import IntelligenceItem, InjectionResult  # noqa: E402
from intelligence_sources._common import _table_has_column  # noqa: E402
from intelligence_sources._recording import record_pattern_usage  # noqa: E402
import intelligence_sources._recording as _recording  # noqa: E402


# ---------------------------------------------------------------------------
# DB schemas — mirror the live shapes (see dispatch background / ADR-007)
# ---------------------------------------------------------------------------

_PROJECT_SCHEMA = """
CREATE TABLE pattern_usage (
    pattern_id TEXT NOT NULL,
    pattern_title TEXT,
    pattern_hash TEXT,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_offered TEXT,
    confidence REAL DEFAULT 0.0,
    created_at TEXT,
    updated_at TEXT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    PRIMARY KEY (pattern_id, project_id)
);
CREATE UNIQUE INDEX ux_pattern_usage_pid ON pattern_usage (project_id, pattern_id);

CREATE TABLE dispatch_pattern_offered (
    dispatch_id TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    pattern_title TEXT,
    offered_at TEXT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    PRIMARY KEY (dispatch_id, pattern_id, project_id)
);
CREATE UNIQUE INDEX ux_dispatch_pattern_offered_pid
    ON dispatch_pattern_offered (project_id, dispatch_id, pattern_id);
"""

_LEGACY_SCHEMA = """
CREATE TABLE pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT,
    pattern_hash TEXT,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_offered TEXT,
    confidence REAL DEFAULT 0.0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE dispatch_pattern_offered (
    dispatch_id TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    pattern_title TEXT,
    offered_at TEXT,
    PRIMARY KEY (dispatch_id, pattern_id)
);
"""


def _make_db(schema_sql: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_sql)
    conn.commit()
    return conn


def _has_column_fn(conn: sqlite3.Connection):
    return lambda table, column: _table_has_column(conn, table, column)


def _make_item(
    item_id: str = "test-pattern-1",
    title: str = "Test pattern",
    confidence: float = 0.8,
) -> IntelligenceItem:
    # item_id intentionally has no intel_sp_/intel_ap_ prefix so
    # _stamp_source_dispatch_id no-ops without needing success_patterns/antipatterns tables.
    return IntelligenceItem(
        item_id=item_id,
        item_class="proven_pattern",
        title=title,
        content="Some pattern content",
        confidence=confidence,
        evidence_count=2,
        last_seen="2026-07-12T00:00:00Z",
        scope_tags=["backend-developer"],
    )


def _make_result(items, dispatch_id: str = "dispatch-1") -> InjectionResult:
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-07-12T00:00:00Z",
        items=items,
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id=dispatch_id,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _assert_no_swallowed_warning(caplog) -> None:
    for record in caplog.records:
        assert "Failed to record pattern usage" not in record.getMessage(), (
            f"UPSERT conflict-target mismatch resurfaced: {record.getMessage()}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPatternUsageUpsertProjectSchema:
    """ADR-007 project-stamped tables: composite-PK ON CONFLICT targets must fire cleanly."""

    def test_both_tables_receive_rows(self, caplog):
        caplog.set_level(logging.WARNING, logger="intelligence_sources._recording")
        conn = _make_db(_PROJECT_SCHEMA)
        item = _make_item()
        result = _make_result([item], dispatch_id="dispatch-both")

        record_pattern_usage(result, conn, _has_column_fn(conn))

        assert _count(conn, "pattern_usage") == 1
        assert _count(conn, "dispatch_pattern_offered") == 1
        _assert_no_swallowed_warning(caplog)

    def test_reoffer_of_seen_pattern_updates_not_raises(self, caplog):
        """The bug: a re-offer of a previously-seen pattern hit ON CONFLICT(pattern_id),
        which does not match the composite PK, raised sqlite3.Error, and was swallowed."""
        caplog.set_level(logging.WARNING, logger="intelligence_sources._recording")
        conn = _make_db(_PROJECT_SCHEMA)
        item = _make_item()

        record_pattern_usage(_make_result([item], dispatch_id="dispatch-a"), conn, _has_column_fn(conn))
        record_pattern_usage(_make_result([item], dispatch_id="dispatch-b"), conn, _has_column_fn(conn))

        assert _count(conn, "pattern_usage") == 1, "re-offer must UPDATE the existing row, not duplicate"
        assert _count(conn, "dispatch_pattern_offered") == 2, "each distinct dispatch gets its own offering row"
        _assert_no_swallowed_warning(caplog)

    def test_idempotent_same_dispatch(self, caplog):
        caplog.set_level(logging.WARNING, logger="intelligence_sources._recording")
        conn = _make_db(_PROJECT_SCHEMA)
        item = _make_item()
        result = _make_result([item], dispatch_id="dispatch-repeat")

        record_pattern_usage(result, conn, _has_column_fn(conn))
        record_pattern_usage(result, conn, _has_column_fn(conn))

        assert _count(conn, "pattern_usage") == 1
        assert _count(conn, "dispatch_pattern_offered") == 1
        _assert_no_swallowed_warning(caplog)

        pu_row = conn.execute(
            "SELECT updated_at FROM pattern_usage WHERE pattern_id = ?", (item.item_id,)
        ).fetchone()
        dpo_row = conn.execute(
            "SELECT offered_at FROM dispatch_pattern_offered WHERE dispatch_id = ? AND pattern_id = ?",
            (result.dispatch_id, item.item_id),
        ).fetchone()
        assert pu_row is not None and pu_row[0]
        assert dpo_row is not None and dpo_row[0]

    def test_cross_project_isolation(self, caplog, monkeypatch):
        """Same pattern_id recorded under two different project_ids coexists as two rows
        (the composite PK keeps tenants isolated); neither UPSERT clobbers the other."""
        caplog.set_level(logging.WARNING, logger="intelligence_sources._recording")
        conn = _make_db(_PROJECT_SCHEMA)
        item = _make_item(item_id="shared-pattern")

        monkeypatch.setattr(_recording, "current_project_id", lambda: "project-a")
        record_pattern_usage(_make_result([item], dispatch_id="dispatch-pa"), conn, _has_column_fn(conn))

        monkeypatch.setattr(_recording, "current_project_id", lambda: "project-b")
        record_pattern_usage(_make_result([item], dispatch_id="dispatch-pb"), conn, _has_column_fn(conn))

        pu_rows = conn.execute(
            "SELECT project_id FROM pattern_usage WHERE pattern_id = ? ORDER BY project_id",
            (item.item_id,),
        ).fetchall()
        assert [r[0] for r in pu_rows] == ["project-a", "project-b"]

        dpo_rows = conn.execute(
            "SELECT project_id FROM dispatch_pattern_offered WHERE pattern_id = ? ORDER BY project_id",
            (item.item_id,),
        ).fetchall()
        assert [r[0] for r in dpo_rows] == ["project-a", "project-b"]
        _assert_no_swallowed_warning(caplog)


class TestPatternUsageUpsertLegacySchema:
    """Legacy single-column-PK stores (no project_id) must keep working via the else branch."""

    def test_legacy_schema_writes_via_else_branch(self, caplog):
        caplog.set_level(logging.WARNING, logger="intelligence_sources._recording")
        conn = _make_db(_LEGACY_SCHEMA)
        item = _make_item(item_id="legacy-pattern")
        result = _make_result([item], dispatch_id="dispatch-legacy")

        record_pattern_usage(result, conn, _has_column_fn(conn))
        record_pattern_usage(result, conn, _has_column_fn(conn))  # idempotent re-run

        assert _count(conn, "pattern_usage") == 1
        assert _count(conn, "dispatch_pattern_offered") == 1
        _assert_no_swallowed_warning(caplog)
