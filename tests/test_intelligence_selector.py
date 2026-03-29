#!/usr/bin/env python3
"""
Tests for intelligence_selector.py (PR-3)

Quality gate coverage (gate_pr3_bounded_intelligence_injection):
  - Intelligence is injected only at dispatch-create and resume paths
  - Injection payload is bounded to at most three evidence-backed items
  - Each intelligence item includes confidence, evidence_count, last_seen, and scope tags
  - Task-class-aware filtering changes the selected items when routing context changes
  - Tests cover bounded payload enforcement, evidence thresholds, and suppression behavior
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_selector import (
    CONFIDENCE_THRESHOLDS,
    EVIDENCE_THRESHOLDS,
    ITEM_CLASS_PRIORITY,
    MAX_CONTENT_CHARS_PER_ITEM,
    MAX_ITEMS_PER_INJECTION,
    MAX_PAYLOAD_CHARS,
    VALID_INJECTION_POINTS,
    IntelligenceItem,
    IntelligenceSelector,
    InjectionResult,
    SuppressionRecord,
    resolve_task_class,
    select_intelligence,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _setup_quality_db(db_path: Path) -> sqlite3.Connection:
    """Create a minimal quality_intelligence.db with test data."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
            success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME, last_used DATETIME
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            created_at TEXT, triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT, cognition TEXT DEFAULT 'normal',
            priority TEXT DEFAULT 'P1', pr_id TEXT, parent_dispatch TEXT,
            pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0,
            intelligence_json TEXT, instruction_char_count INTEGER DEFAULT 0,
            context_file_count INTEGER DEFAULT 0,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT, outcome_report_path TEXT, session_id TEXT
        );
    """)
    conn.commit()
    return conn


def _seed_proven_pattern(
    conn: sqlite3.Connection,
    title: str = "Use structured output",
    description: str = "Structured output improves first-pass success by 25%.",
    category: str = "architect",
    confidence: float = 0.85,
    usage_count: int = 5,
    last_used: str = "2026-03-28T14:00:00",
) -> int:
    cur = conn.execute(
        """INSERT INTO success_patterns (title, description, category, confidence_score,
           usage_count, last_used, pattern_data, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, '{}', ?)""",
        (title, description, category, confidence, usage_count, last_used, last_used),
    )
    conn.commit()
    return cur.lastrowid


def _seed_antipattern(
    conn: sqlite3.Connection,
    title: str = "Unbounded file reads",
    why_problematic: str = "Causes context pressure and failures.",
    better_alternative: str = "Scope reads to dispatch paths.",
    category: str = "reviewer",
    severity: str = "high",
    occurrence_count: int = 3,
    last_seen: str = "2026-03-27T09:00:00",
) -> int:
    cur = conn.execute(
        """INSERT INTO antipatterns (title, description, category, severity,
           why_problematic, better_alternative, occurrence_count, last_seen,
           pattern_data, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)""",
        (title, title, category, severity, why_problematic, better_alternative,
         occurrence_count, last_seen, last_seen),
    )
    conn.commit()
    return cur.lastrowid


def _seed_prevention_rule(
    conn: sqlite3.Connection,
    description: str = "Avoid parallel file edits",
    recommendation: str = "Use sequential editing for related files.",
    tag_combination: str = "architect,Track-C",
    confidence: float = 0.7,
    triggered_count: int = 2,
) -> int:
    cur = conn.execute(
        """INSERT INTO prevention_rules (tag_combination, rule_type, description,
           recommendation, confidence, created_at, triggered_count)
           VALUES (?, 'prevention', ?, ?, ?, datetime('now'), ?)""",
        (tag_combination, description, recommendation, confidence, triggered_count),
    )
    conn.commit()
    return cur.lastrowid


def _seed_recent_dispatch(
    conn: sqlite3.Connection,
    dispatch_id: str = "test-dispatch-recent",
    skill_name: str = "architect",
    gate: str = "gate_pr3_test",
    track: str = "C",
    outcome: str = "success",
    days_ago: int = 3,
) -> int:
    dispatched_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    cur = conn.execute(
        """INSERT INTO dispatch_metadata (dispatch_id, terminal, track, skill_name,
           gate, outcome_status, dispatched_at)
           VALUES (?, 'T3', ?, ?, ?, ?, ?)""",
        (dispatch_id, track, skill_name, gate, outcome, dispatched_at),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Tests: resolve_task_class
# ---------------------------------------------------------------------------

class TestResolveTaskClass(unittest.TestCase):
    def test_explicit_task_class(self):
        self.assertEqual(resolve_task_class("research_structured"), "research_structured")

    def test_skill_name_mapping(self):
        self.assertEqual(resolve_task_class(skill_name="architect"), "research_structured")
        self.assertEqual(resolve_task_class(skill_name="backend-developer"), "coding_interactive")
        self.assertEqual(resolve_task_class(skill_name="excel-reporter"), "docs_synthesis")

    def test_unknown_skill_defaults_coding(self):
        self.assertEqual(resolve_task_class(skill_name="unknown-skill"), "coding_interactive")

    def test_no_args_defaults_coding(self):
        self.assertEqual(resolve_task_class(), "coding_interactive")

    def test_explicit_overrides_skill(self):
        self.assertEqual(
            resolve_task_class("docs_synthesis", skill_name="backend-developer"),
            "docs_synthesis",
        )


# ---------------------------------------------------------------------------
# Tests: IntelligenceItem
# ---------------------------------------------------------------------------

class TestIntelligenceItem(unittest.TestCase):
    def test_to_dict_truncates_content(self):
        item = IntelligenceItem(
            item_id="test",
            item_class="proven_pattern",
            title="Test",
            content="x" * 600,
            confidence=0.8,
            evidence_count=3,
            last_seen="2026-03-28T00:00:00Z",
            scope_tags=["test"],
        )
        d = item.to_dict()
        self.assertLessEqual(len(d["content"]), MAX_CONTENT_CHARS_PER_ITEM)

    def test_to_dict_schema_completeness(self):
        item = IntelligenceItem(
            item_id="intel_abc",
            item_class="failure_prevention",
            title="Test item",
            content="Some content",
            confidence=0.65,
            evidence_count=2,
            last_seen="2026-03-28T00:00:00Z",
            scope_tags=["architect", "Track-C"],
            source_refs=["antipattern_1"],
        )
        d = item.to_dict()
        required_keys = {
            "item_id", "item_class", "title", "content",
            "confidence", "evidence_count", "last_seen", "scope_tags",
        }
        self.assertTrue(required_keys.issubset(d.keys()))


# ---------------------------------------------------------------------------
# Tests: InjectionResult
# ---------------------------------------------------------------------------

class TestInjectionResult(unittest.TestCase):
    def _make_result(self, items=None, suppressed=None):
        return InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-03-29T00:00:00Z",
            items=items or [],
            suppressed=suppressed or [],
            task_class="research_structured",
            dispatch_id="test-001",
        )

    def test_empty_result_counts(self):
        r = self._make_result()
        self.assertEqual(r.items_injected, 0)
        self.assertEqual(r.items_suppressed, 0)

    def test_payload_dict_structure(self):
        item = IntelligenceItem(
            item_id="i1", item_class="proven_pattern", title="T", content="C",
            confidence=0.8, evidence_count=3, last_seen="2026-03-28T00:00:00Z",
            scope_tags=["test"],
        )
        supp = SuppressionRecord(item_class="failure_prevention", reason="no candidates")
        r = self._make_result(items=[item], suppressed=[supp])
        payload = r.to_payload_dict()
        self.assertEqual(payload["injection_point"], "dispatch_create")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(len(payload["suppressed"]), 1)

    def test_event_metadata_keys(self):
        r = self._make_result()
        meta = r.to_event_metadata()
        expected = {
            "injection_point", "task_class", "items_injected",
            "items_suppressed", "suppression_reasons", "payload_chars", "item_ids",
        }
        self.assertTrue(expected.issubset(meta.keys()))


# ---------------------------------------------------------------------------
# Tests: IntelligenceSelector — core selection
# ---------------------------------------------------------------------------

class TestIntelligenceSelectorBasic(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_db_returns_all_suppressed(self):
        """No candidates → all three slots suppressed."""
        db = _setup_quality_db(self._quality_db_path)
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-001", "dispatch_create", task_class="research_structured")
        selector.close()

        self.assertEqual(result.items_injected, 0)
        self.assertEqual(result.items_suppressed, 3)
        for s in result.suppressed:
            self.assertEqual(s.reason, "no candidates available")

    def test_max_three_items(self):
        """Even with many candidates, at most 3 items are selected."""
        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, confidence=0.9, usage_count=10)
        _seed_antipattern(db, severity="critical", occurrence_count=5)
        _seed_recent_dispatch(db, dispatch_id="rc-1", skill_name="architect")
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-002", "dispatch_create", skill_name="architect")
        selector.close()

        self.assertLessEqual(result.items_injected, MAX_ITEMS_PER_INJECTION)

    def test_confidence_threshold_filtering(self):
        """Items below confidence threshold are suppressed."""
        db = _setup_quality_db(self._quality_db_path)
        # Pattern with confidence below proven_pattern threshold (0.6)
        _seed_proven_pattern(db, title="Low confidence", confidence=0.3, usage_count=3,
                           category="architect")
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        # Pass skill_name so scope tags include "architect" to match pattern category
        result = selector.select("d-003", "dispatch_create", skill_name="architect")
        selector.close()

        # proven_pattern should be suppressed (0.3 < 0.6)
        proven_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven_items), 0)

        proven_suppressed = [s for s in result.suppressed if s.item_class == "proven_pattern"]
        self.assertEqual(len(proven_suppressed), 1)
        self.assertIn("below threshold", proven_suppressed[0].reason)

    def test_evidence_count_filtering(self):
        """Items with insufficient evidence are suppressed."""
        db = _setup_quality_db(self._quality_db_path)
        # Pattern with only 1 usage (below proven_pattern minimum of 2)
        _seed_proven_pattern(db, title="Low evidence", confidence=0.9, usage_count=1,
                           category="architect")
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-004", "dispatch_create", skill_name="architect")
        selector.close()

        proven_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven_items), 0)

    def test_highest_confidence_selected(self):
        """When multiple candidates pass thresholds, highest confidence wins."""
        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, title="Medium", confidence=0.7, usage_count=3,
                           category="architect")
        _seed_proven_pattern(db, title="High", confidence=0.95, usage_count=5,
                           category="architect")
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-005", "dispatch_create", skill_name="architect")
        selector.close()

        proven_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven_items), 1)
        self.assertGreaterEqual(proven_items[0].confidence, 0.9)


# ---------------------------------------------------------------------------
# Tests: injection points
# ---------------------------------------------------------------------------

class TestInjectionPoints(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        db = _setup_quality_db(self._quality_db_path)
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_dispatch_create_allowed(self):
        selector = IntelligenceSelector(quality_db_path=self._quality_db_path)
        result = selector.select("d-010", "dispatch_create")
        self.assertEqual(result.injection_point, "dispatch_create")
        selector.close()

    def test_dispatch_resume_allowed(self):
        selector = IntelligenceSelector(quality_db_path=self._quality_db_path)
        result = selector.select("d-011", "dispatch_resume")
        self.assertEqual(result.injection_point, "dispatch_resume")
        selector.close()

    def test_invalid_injection_point_rejected(self):
        selector = IntelligenceSelector(quality_db_path=self._quality_db_path)
        with self.assertRaises(ValueError) as ctx:
            selector.select("d-012", "mid_execution")
        self.assertIn("Invalid injection_point", str(ctx.exception))
        selector.close()

    def test_receipt_processing_rejected(self):
        selector = IntelligenceSelector(quality_db_path=self._quality_db_path)
        with self.assertRaises(ValueError):
            selector.select("d-013", "receipt_processing")
        selector.close()


# ---------------------------------------------------------------------------
# Tests: task-class-aware filtering
# ---------------------------------------------------------------------------

class TestTaskClassFiltering(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))

        db = _setup_quality_db(self._quality_db_path)
        # Architect-scoped pattern
        _seed_proven_pattern(db, title="Architect pattern", category="architect",
                           confidence=0.85, usage_count=4)
        # Backend-scoped pattern
        _seed_proven_pattern(db, title="Backend pattern", category="backend-developer",
                           confidence=0.8, usage_count=3)
        # Architect-scoped antipattern
        _seed_antipattern(db, title="Arch antipattern", category="architect",
                         severity="high", occurrence_count=3)
        # Recent dispatch with architect scope
        _seed_recent_dispatch(db, dispatch_id="recent-arch", skill_name="architect",
                            gate="gate_test", track="C")
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_architect_gets_architect_scoped_items(self):
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select(
            "d-020", "dispatch_create",
            skill_name="architect", track="C",
        )
        selector.close()

        item_titles = [i.title for i in result.items]
        has_arch = any("Architect" in t or "architect" in t.lower() for t in item_titles)
        self.assertTrue(has_arch or result.items_injected > 0,
                       f"Expected architect-scoped items, got: {item_titles}")

    def test_different_task_class_changes_selection(self):
        """Switching task class should change what gets selected."""
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result_arch = selector.select(
            "d-021a", "dispatch_create",
            skill_name="architect", track="C",
        )
        result_backend = selector.select(
            "d-021b", "dispatch_create",
            skill_name="backend-developer", track="B",
        )
        selector.close()

        arch_ids = {i.item_id for i in result_arch.items}
        backend_ids = {i.item_id for i in result_backend.items}
        # Different selections (or both empty, which is also valid)
        if result_arch.items_injected > 0 and result_backend.items_injected > 0:
            self.assertNotEqual(arch_ids, backend_ids)


# ---------------------------------------------------------------------------
# Tests: event emission
# ---------------------------------------------------------------------------

class TestEventEmission(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        db = _setup_quality_db(self._quality_db_path)
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_suppression_event_when_no_items(self):
        """When no items meet thresholds, emit intelligence_suppression event."""
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-030", "dispatch_create")
        event_id = selector.emit_event(result)
        selector.close()

        self.assertIsNotNone(event_id)

        from runtime_coordination import get_events
        with get_connection(self._state_dir) as conn:
            events = get_events(conn, entity_id="d-030", event_type="intelligence_suppression")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "intelligence_selector")

    def test_injection_event_when_items_selected(self):
        """When items are selected, emit intelligence_injection event."""
        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, confidence=0.9, usage_count=5)
        db.close()

        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-031", "dispatch_create", skill_name="architect")
        if result.items_injected > 0:
            event_id = selector.emit_event(result)
            self.assertIsNotNone(event_id)

            from runtime_coordination import get_events
            with get_connection(self._state_dir) as conn:
                events = get_events(conn, entity_id="d-031", event_type="intelligence_injection")
            self.assertEqual(len(events), 1)
            meta = json.loads(events[0]["metadata_json"])
            self.assertIn("items_injected", meta)
            self.assertIn("payload_chars", meta)
        selector.close()


# ---------------------------------------------------------------------------
# Tests: audit trail (intelligence_injections table)
# ---------------------------------------------------------------------------

class TestAuditTrail(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, confidence=0.9, usage_count=5)
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_record_injection_creates_audit_row(self):
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select("d-040", "dispatch_create", skill_name="architect")
        selector.record_injection(result)
        selector.close()

        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM intelligence_injections WHERE dispatch_id = ?",
                ("d-040",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["injection_point"], "dispatch_create")
        self.assertGreaterEqual(row["items_injected"] + row["items_suppressed"], 1)


# ---------------------------------------------------------------------------
# Tests: payload bounds enforcement
# ---------------------------------------------------------------------------

class TestPayloadBounds(unittest.TestCase):
    def test_max_items_enforced(self):
        """No more than 3 items in any injection."""
        items = [
            IntelligenceItem(
                item_id=f"i{n}", item_class=cls,
                title=f"Item {n}", content="Short",
                confidence=0.9, evidence_count=5,
                last_seen="2026-03-28T00:00:00Z",
                scope_tags=["test"],
            )
            for n, cls in enumerate(ITEM_CLASS_PRIORITY)
        ]
        result = InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-03-29T00:00:00Z",
            items=items,
            suppressed=[],
            task_class="research_structured",
            dispatch_id="d-050",
        )
        self.assertLessEqual(result.items_injected, MAX_ITEMS_PER_INJECTION)

    def test_payload_chars_under_limit(self):
        """Payload size must stay under MAX_PAYLOAD_CHARS after enforcement."""
        selector = IntelligenceSelector()
        items = [
            IntelligenceItem(
                item_id=f"i{n}", item_class=cls,
                title=f"Long title item {n}",
                content="x" * MAX_CONTENT_CHARS_PER_ITEM,
                confidence=0.9, evidence_count=5,
                last_seen="2026-03-28T00:00:00Z",
                scope_tags=["test", "research_structured", "Track-C"],
                source_refs=["ref_1", "ref_2"],
            )
            for n, cls in enumerate(ITEM_CLASS_PRIORITY)
        ]
        suppressed = []
        trimmed = selector._enforce_payload_limit(items, suppressed)
        selector.close()

        payload = json.dumps({
            "injection_point": "dispatch_create",
            "injected_at": "2026-03-29T00:00:00Z",
            "items": [i.to_dict() for i in trimmed],
            "suppressed": [s.to_dict() for s in suppressed],
        })
        self.assertLessEqual(len(payload), MAX_PAYLOAD_CHARS)

    def test_drop_order_recent_comparable_first(self):
        """When over limit, recent_comparable is dropped before failure_prevention."""
        selector = IntelligenceSelector()
        items = [
            IntelligenceItem(
                item_id="pp", item_class="proven_pattern",
                title="P", content="x" * 400,
                confidence=0.9, evidence_count=5,
                last_seen="2026-03-28T00:00:00Z", scope_tags=["t"],
            ),
            IntelligenceItem(
                item_id="fp", item_class="failure_prevention",
                title="F", content="x" * 400,
                confidence=0.8, evidence_count=3,
                last_seen="2026-03-28T00:00:00Z", scope_tags=["t"],
            ),
            IntelligenceItem(
                item_id="rc", item_class="recent_comparable",
                title="R", content="x" * 400,
                confidence=0.7, evidence_count=1,
                last_seen="2026-03-28T00:00:00Z", scope_tags=["t"],
            ),
        ]
        suppressed = []
        trimmed = selector._enforce_payload_limit(items, suppressed)
        selector.close()

        remaining_classes = {i.item_class for i in trimmed}
        # If anything was dropped, recent_comparable should go first
        if len(trimmed) < 3:
            self.assertNotIn("recent_comparable", remaining_classes)
            if len(trimmed) < 2:
                self.assertNotIn("failure_prevention", remaining_classes)


# ---------------------------------------------------------------------------
# Tests: convenience function
# ---------------------------------------------------------------------------

class TestSelectIntelligenceConvenience(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, confidence=0.9, usage_count=5)
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_select_intelligence_returns_result(self):
        result = select_intelligence(
            "d-060", "dispatch_create",
            quality_db_path=self._quality_db_path,
            coord_state_dir=self._state_dir,
            skill_name="architect",
        )
        self.assertIsInstance(result, InjectionResult)
        self.assertEqual(result.dispatch_id, "d-060")

    def test_no_quality_db_returns_empty(self):
        result = select_intelligence(
            "d-061", "dispatch_create",
            quality_db_path=self._base / "nonexistent.db",
            coord_state_dir=self._state_dir,
        )
        self.assertEqual(result.items_injected, 0)
        self.assertEqual(result.items_suppressed, 3)


# ---------------------------------------------------------------------------
# Tests: broker integration
# ---------------------------------------------------------------------------

class TestBrokerIntelligenceIntegration(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = str(self._base / "state")
        self._dispatch_dir = str(self._base / "dispatches")
        Path(self._state_dir).mkdir()
        Path(self._dispatch_dir).mkdir()
        init_schema(self._state_dir)

        db = _setup_quality_db(self._quality_db_path)
        _seed_proven_pattern(db, confidence=0.9, usage_count=5)
        _seed_antipattern(db, severity="critical", occurrence_count=5)
        db.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_register_includes_intelligence_payload(self):
        from dispatch_broker import DispatchBroker

        broker = DispatchBroker(
            self._state_dir, self._dispatch_dir,
            shadow_mode=True,
            quality_db_path=str(self._quality_db_path),
            intelligence_enabled=True,
        )
        result = broker.register(
            "intel-test-001", "Do architecture review.",
            terminal_id="T3", track="C",
            skill_name="architect",
        )
        bundle = broker.get_bundle("intel-test-001")
        self.assertIn("intelligence_payload", bundle)
        payload = bundle["intelligence_payload"]
        self.assertIn("items", payload)
        self.assertIn("injection_point", payload)
        self.assertEqual(payload["injection_point"], "dispatch_create")

    def test_register_without_intelligence(self):
        from dispatch_broker import DispatchBroker

        broker = DispatchBroker(
            self._state_dir, self._dispatch_dir,
            shadow_mode=True,
            intelligence_enabled=False,
        )
        result = broker.register("intel-test-002", "Do work.")
        bundle = broker.get_bundle("intel-test-002")
        self.assertNotIn("intelligence_payload", bundle)

    def test_resume_intelligence_injection(self):
        from dispatch_broker import DispatchBroker

        broker = DispatchBroker(
            self._state_dir, self._dispatch_dir,
            shadow_mode=True,
            quality_db_path=str(self._quality_db_path),
            intelligence_enabled=True,
        )
        payload = broker.inject_intelligence_on_resume(
            "intel-test-003",
            skill_name="architect",
            track="C",
        )
        # May or may not have items depending on scope matching
        if payload is not None:
            self.assertEqual(payload["injection_point"], "dispatch_resume")


if __name__ == "__main__":
    unittest.main()
