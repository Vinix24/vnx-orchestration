#!/usr/bin/env python3
"""Regression: stamp_source_dispatch_ids called for all 4 record_injection call sites (OI-1341).

Before this fix, stamp_source_dispatch_ids was only called in
skill_injection._build_intelligence_section; the other three production callers
(dispatch_broker.register_dispatch, dispatch_broker.select_intelligence_for_recovery,
mixed_execution_router._inject_intelligence) called record_injection without a
subsequent stamp.  Receipt-driven decay could not find freshly-injected patterns
from those paths because source_dispatch_ids was never populated.

Fix: stamp_source_dispatch_ids is now embedded in record_injection itself so all
callers get it automatically. Each test below simulates one of the 4 call-site
contexts.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_LIB = TESTS_DIR.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT, category TEXT, title TEXT, description TEXT,
    pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
    success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
    avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
    source_dispatch_ids TEXT, source_receipts TEXT,
    first_seen DATETIME, last_used DATETIME,
    valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT, category TEXT, title TEXT, description TEXT,
    pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
    better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
    avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
    source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
    valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS prevention_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_combination TEXT, rule_type TEXT, description TEXT,
    recommendation TEXT, confidence REAL DEFAULT 0.0,
    created_at TEXT, triggered_count INTEGER DEFAULT 0,
    last_triggered TEXT
);
CREATE TABLE IF NOT EXISTS pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT, pattern_hash TEXT,
    used_count INTEGER DEFAULT 0, ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
    last_offered TEXT, confidence REAL DEFAULT 0.0,
    created_at TEXT, updated_at TEXT
);
"""


def _make_quality_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _seed_success_pattern(conn: sqlite3.Connection, title: str) -> int:
    cur = conn.execute(
        "INSERT INTO success_patterns "
        "(title, description, category, confidence_score, usage_count, first_seen, last_used, pattern_data) "
        "VALUES (?, ?, 'backend-developer', 0.9, 5, '2026-01-01', '2026-05-01', '{}')",
        (title, f"Description: {title}"),
    )
    conn.commit()
    return cur.lastrowid


def _seed_antipattern(conn: sqlite3.Connection, title: str) -> int:
    cur = conn.execute(
        "INSERT INTO antipatterns "
        "(title, description, category, severity, occurrence_count, first_seen, last_seen, pattern_data) "
        "VALUES (?, ?, 'backend-developer', 'high', 3, '2026-01-01', '2026-05-01', '{}')",
        (title, f"Description: {title}"),
    )
    conn.commit()
    return cur.lastrowid


def _make_sp_item(row_id: int, title: str):
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id=f"intel_sp_{row_id}",
        item_class="proven_pattern",
        title=title,
        content=f"Description: {title}",
        confidence=0.9,
        evidence_count=5,
        last_seen="2026-05-07T00:00:00Z",
        scope_tags=["backend-developer"],
    )


def _make_ap_item(row_id: int, title: str):
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id=f"intel_ap_{row_id}",
        item_class="failure_prevention",
        title=title,
        content=f"Description: {title}",
        confidence=0.85,
        evidence_count=3,
        last_seen="2026-05-07T00:00:00Z",
        scope_tags=["backend-developer"],
    )


def _make_result(items, dispatch_id: str, injection_point: str = "dispatch_create"):
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point=injection_point,
        injected_at="2026-05-07T00:00:00Z",
        items=items,
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id=dispatch_id,
    )


def _read_source_ids(conn: sqlite3.Connection, table: str, row_id: int) -> list:
    row = conn.execute(
        f"SELECT source_dispatch_ids FROM {table} WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []


def _make_selector(quality_db_path: Path, state_dir: Path):
    from intelligence_selector import IntelligenceSelector
    return IntelligenceSelector(
        quality_db_path=quality_db_path,
        coord_db_state_dir=state_dir,
    )


def _setup(tmp_path: Path):
    from runtime_coordination import init_schema
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    init_schema(str(state_dir))
    quality_db_path = tmp_path / "quality_intelligence.db"
    conn = _make_quality_db(quality_db_path)
    return state_dir, quality_db_path, conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStampSourceDispatchIdsAllCallers:
    """record_injection stamps source_dispatch_ids for all 4 production call sites."""

    def test_site1_skill_injection_proven_pattern(self, tmp_path):
        """Site 1: skill_injection._build_intelligence_section (coord_state_dir as kwarg)."""
        state_dir, qdb, conn = _setup(tmp_path)
        row_id = _seed_success_pattern(conn, "Write tests before commit")
        item = _make_sp_item(row_id, "Write tests before commit")
        result = _make_result([item], "dispatch-site1", "dispatch_create")

        selector = _make_selector(qdb, state_dir)
        # skill_injection passes coord_state_dir explicitly as a kwarg
        selector.record_injection(result, coord_state_dir=state_dir)
        selector.close()

        ids = _read_source_ids(conn, "success_patterns", row_id)
        assert "dispatch-site1" in ids, f"source_dispatch_ids not stamped; got {ids}"

    def test_site2_dispatch_broker_register_proven_pattern(self, tmp_path):
        """Site 2: dispatch_broker.register_dispatch (coord_state_dir from constructor, project_id="vnx-dev")."""
        state_dir, qdb, conn = _setup(tmp_path)
        row_id = _seed_success_pattern(conn, "Use structured output")
        item = _make_sp_item(row_id, "Use structured output")
        result = _make_result([item], "dispatch-site2", "dispatch_create")

        selector = _make_selector(qdb, state_dir)
        # dispatch_broker passes coord_db_state_dir to constructor only
        selector.record_injection(result)
        selector.close()

        ids = _read_source_ids(conn, "success_patterns", row_id)
        assert "dispatch-site2" in ids, f"source_dispatch_ids not stamped; got {ids}"

    def test_site3_dispatch_broker_recovery_antipattern(self, tmp_path):
        """Site 3: dispatch_broker.select_intelligence_for_recovery (injection_point=dispatch_resume)."""
        state_dir, qdb, conn = _setup(tmp_path)
        row_id = _seed_antipattern(conn, "Never skip gates")
        item = _make_ap_item(row_id, "Never skip gates")
        result = _make_result([item], "dispatch-site3", "dispatch_resume")

        selector = _make_selector(qdb, state_dir)
        selector.record_injection(result)
        selector.close()

        ids = _read_source_ids(conn, "antipatterns", row_id)
        assert "dispatch-site3" in ids, f"source_dispatch_ids not stamped; got {ids}"

    def test_site4_mixed_execution_router_proven_pattern(self, tmp_path):
        """Site 4: mixed_execution_router._inject_intelligence path."""
        state_dir, qdb, conn = _setup(tmp_path)
        row_id = _seed_success_pattern(conn, "Emit routing decision event")
        item = _make_sp_item(row_id, "Emit routing decision event")
        result = _make_result([item], "dispatch-site4", "dispatch_create")

        selector = _make_selector(qdb, state_dir)
        selector.record_injection(result)
        selector.close()

        ids = _read_source_ids(conn, "success_patterns", row_id)
        assert "dispatch-site4" in ids, f"source_dispatch_ids not stamped; got {ids}"

    def test_stamp_is_idempotent(self, tmp_path):
        """Calling record_injection twice does not duplicate the dispatch_id."""
        state_dir, qdb, conn = _setup(tmp_path)
        row_id = _seed_success_pattern(conn, "Idempotent stamp check")
        item = _make_sp_item(row_id, "Idempotent stamp check")
        result = _make_result([item], "dispatch-idempotent", "dispatch_create")

        selector = _make_selector(qdb, state_dir)
        selector.record_injection(result)
        selector.record_injection(result)
        selector.close()

        ids = _read_source_ids(conn, "success_patterns", row_id)
        assert ids.count("dispatch-idempotent") == 1, (
            f"dispatch_id should appear exactly once; got {ids}"
        )

    def test_mixed_item_types_both_stamped(self, tmp_path):
        """A single injection with both proven_pattern and failure_prevention items stamps both tables."""
        state_dir, qdb, conn = _setup(tmp_path)
        sp_id = _seed_success_pattern(conn, "Multi-type SP")
        ap_id = _seed_antipattern(conn, "Multi-type AP")
        sp_item = _make_sp_item(sp_id, "Multi-type SP")
        ap_item = _make_ap_item(ap_id, "Multi-type AP")
        result = _make_result([sp_item, ap_item], "dispatch-multitype", "dispatch_create")

        selector = _make_selector(qdb, state_dir)
        selector.record_injection(result)
        selector.close()

        sp_ids = _read_source_ids(conn, "success_patterns", sp_id)
        ap_ids = _read_source_ids(conn, "antipatterns", ap_id)
        assert "dispatch-multitype" in sp_ids, f"SP not stamped; got {sp_ids}"
        assert "dispatch-multitype" in ap_ids, f"AP not stamped; got {ap_ids}"
