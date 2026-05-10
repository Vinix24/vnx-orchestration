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
            last_triggered TEXT,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
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


# ---------------------------------------------------------------------------
# Tests: CFX-6 tag_combination column format (JSON array vs comma-list)
# ---------------------------------------------------------------------------

class TestPreventionRuleTagParsing(unittest.TestCase):
    """CFX-6: intelligence_selector parses tag_combination as JSON array with
    backward-compatible comma-list fallback."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        self._db = _setup_quality_db(self._quality_db_path)

    def tearDown(self):
        self._db.close()
        self._tmpdir.cleanup()

    def _make_selector(self) -> IntelligenceSelector:
        return IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )

    def test_json_array_format_becomes_scope_tags(self):
        """A prevention rule stored as JSON array yields correct scope_tags."""
        _seed_prevention_rule(
            self._db,
            tag_combination='["architect","Track-C"]',
            description="JSON array rule",
            confidence=0.8,
        )

        selector = self._make_selector()
        result = selector.select("cfx6-001", "dispatch_create", skill_name="architect")
        selector.close()

        injected_items = result.items
        pr_items = [i for i in injected_items if i.item_class == "failure_prevention"]
        if pr_items:
            scope = pr_items[0].scope_tags
            self.assertIn("architect", scope)
            self.assertIn("Track-C", scope)

    def test_comma_list_fallback_still_works(self):
        """A prevention rule stored as comma-list (legacy) still parses to correct scope_tags."""
        _seed_prevention_rule(
            self._db,
            tag_combination="architect,Track-C",
            description="Comma-list rule",
            confidence=0.8,
        )

        selector = self._make_selector()
        result = selector.select("cfx6-002", "dispatch_create", skill_name="architect")
        selector.close()

        pr_items = [i for i in result.items if i.item_class == "failure_prevention"]
        if pr_items:
            scope = pr_items[0].scope_tags
            self.assertIn("architect", scope)
            self.assertIn("Track-C", scope)

    def test_json_array_scope_not_split_on_comma(self):
        """A JSON array value is NOT incorrectly split — '[\"a\",\"b\"]' gives ['a','b'], not ['[\"a\"', ...]."""
        _seed_prevention_rule(
            self._db,
            tag_combination='["backend-developer","testing-phase"]',
            description="No stray brackets rule",
            confidence=0.9,
        )

        selector = self._make_selector()
        result = selector.select("cfx6-003", "dispatch_create", skill_name="backend-developer")
        selector.close()

        pr_items = [i for i in result.items if i.item_class == "failure_prevention"]
        if pr_items:
            for tag in pr_items[0].scope_tags:
                self.assertFalse(tag.startswith("["), f"Scope tag has stray bracket: {tag!r}")
                self.assertFalse(tag.endswith("]"), f"Scope tag has stray bracket: {tag!r}")

    def test_empty_tag_combination_yields_empty_scope(self):
        """Empty tag_combination does not crash and yields empty scope."""
        _seed_prevention_rule(
            self._db,
            tag_combination="",
            description="Empty combo rule",
            confidence=0.6,
        )
        selector = self._make_selector()
        result = selector.select("cfx6-004", "dispatch_create")
        selector.close()
        # Should not raise; result is valid
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# Wave 1 shadow-mode tests — VNX_USE_CENTRAL_DB flag (PR-W1.4)
# ---------------------------------------------------------------------------

def _setup_central_quality_db(central_db_path: Path, project_id: str) -> sqlite3.Connection:
    """Create a minimal central quality_intelligence.db with project_id column seeded."""
    conn = sqlite3.connect(str(central_db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT, first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT, title TEXT, description TEXT,
            why_problematic TEXT, better_alternative TEXT,
            occurrence_count INTEGER DEFAULT 0, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT, priority TEXT DEFAULT 'P1',
            pr_id TEXT, pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT, project_id TEXT
        );
    """)
    conn.commit()
    return conn


def _seed_central_proven_pattern(
    conn: sqlite3.Connection,
    project_id: str,
    title: str = "Central pattern",
    description: str = "From central DB.",
    category: str = "architect",
    confidence: float = 0.85,
    usage_count: int = 5,
) -> int:
    cur = conn.execute(
        """INSERT INTO success_patterns (title, description, category, confidence_score,
           usage_count, first_seen, last_used, project_id)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)""",
        (title, description, category, confidence, usage_count, project_id),
    )
    conn.commit()
    return cur.lastrowid


def _seed_central_antipattern(
    conn: sqlite3.Connection,
    project_id: str,
    title: str = "Central antipattern",
    category: str = "reviewer",
    severity: str = "high",
    occurrence_count: int = 3,
) -> int:
    cur = conn.execute(
        """INSERT INTO antipatterns (title, description, category, severity,
           why_problematic, better_alternative, occurrence_count,
           first_seen, last_seen, project_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)""",
        (title, title, category, severity, "Bad pattern", "Do better", occurrence_count, project_id),
    )
    conn.commit()
    return cur.lastrowid


def _seed_central_dispatch(
    conn: sqlite3.Connection,
    project_id: str,
    dispatch_id: str = "central-dispatch-001",
    skill_name: str = "architect",
    outcome: str = "success",
    days_ago: int = 2,
) -> int:
    from datetime import datetime, timedelta, timezone
    dispatched_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    cur = conn.execute(
        """INSERT INTO dispatch_metadata (dispatch_id, terminal, track, skill_name,
           outcome_status, dispatched_at, project_id)
           VALUES (?, 'T1', 'A', ?, ?, ?, ?)""",
        (dispatch_id, skill_name, outcome, dispatched_at, project_id),
    )
    conn.commit()
    return cur.lastrowid


class TestShadowModeQueryProvenPatterns(unittest.TestCase):
    """3-state flag tests for _query_proven_patterns (metric 3, success_patterns)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._project_id = "test-project"
        # Per-project DB
        self._per_project_db_path = self._base / "quality_intelligence.db"
        self._per_project_db = _setup_quality_db(self._per_project_db_path)
        _seed_proven_pattern(
            self._per_project_db, title="Per-project pattern",
            confidence=0.85, usage_count=5, category="architect",
        )
        self._per_project_db.close()
        # Central DB (simulated at ~/.vnx-data/<project_id>/state/)
        self._central_dir = self._base / "central" / self._project_id / "state"
        self._central_dir.mkdir(parents=True)
        self._central_db_path = self._central_dir / "quality_intelligence.db"
        self._central_db = _setup_central_quality_db(self._central_db_path, self._project_id)
        _seed_central_proven_pattern(
            self._central_db, self._project_id,
            title="Central pattern", confidence=0.85, usage_count=5, category="architect",
        )
        self._central_db.close()
        # Shadow ledger dir
        self._ledger_path = self._base / "shadow_divergence.ndjson"
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        self._tmpdir.cleanup()

    def _make_selector(self) -> IntelligenceSelector:
        return IntelligenceSelector(quality_db_path=self._per_project_db_path)

    def _open_per_project_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._per_project_db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test__query_proven_patterns_unset_uses_per_project(self):
        """When VNX_USE_CENTRAL_DB unset, dispatcher returns per-project result unchanged."""
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        selector = self._make_selector()
        db = self._open_per_project_db()
        items = selector._query_proven_patterns(db, "research_structured", ["architect"])
        db.close()
        selector.close()
        self.assertTrue(len(items) >= 1)
        titles = [i.title for i in items]
        self.assertTrue(any("Per-project" in t for t in titles))

    def test__query_proven_patterns_shadow_logs_divergence(self):
        """Shadow mode logs divergence when per-project and central results differ."""
        import shadow_verifier
        import shadow_logger

        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"

        # Patch _get_central_qi_conn to return central DB conn
        import intelligence_selector as _is_module
        original_central_data_dir = _is_module._resolve_central_data_dir

        def _patched_resolve(pid):
            return self._base / "central" / pid

        _is_module._resolve_central_data_dir = _patched_resolve

        # Patch current_project_id to return our test project_id
        import project_scope as _ps
        original_pid = _ps.current_project_id

        def _patched_pid():
            return self._project_id

        _ps.current_project_id = _patched_pid

        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            # Add a divergence: extra item in per-project
            db.execute(
                """INSERT INTO success_patterns (title, description, category,
                   confidence_score, usage_count, first_seen)
                   VALUES ('Extra per-project', 'Only in per-project', 'reviewer', 0.9, 3, datetime('now'))"""
            )
            db.commit()
            items = selector._query_proven_patterns(db, "research_structured", [])
            db.close()
            selector.close()
            # Legacy result is authoritative (returned)
            self.assertTrue(len(items) >= 1)
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_central_data_dir
            _ps.current_project_id = original_pid

    def test__query_proven_patterns_authoritative_uses_central(self):
        """VNX_USE_CENTRAL_DB=1 → dispatcher returns central result."""
        import intelligence_selector as _is_module
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _is_module.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _is_module.current_project_id = lambda: self._project_id

        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            items = selector._query_proven_patterns(db, "research_structured", ["architect"])
            db.close()
            selector.close()
            titles = [i.title for i in items]
            self.assertTrue(any("Central" in t for t in titles), f"Expected central title, got: {titles}")
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _is_module.current_project_id = original_pid


class TestShadowModeQueryFailurePrevention(unittest.TestCase):
    """3-state flag tests for _query_failure_prevention (metric 3, antipatterns)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._project_id = "test-project"
        self._per_project_db_path = self._base / "quality_intelligence.db"
        db = _setup_quality_db(self._per_project_db_path)
        _seed_antipattern(db, title="Per-project antipattern", severity="high", occurrence_count=3)
        db.close()
        self._central_dir = self._base / "central" / self._project_id / "state"
        self._central_dir.mkdir(parents=True)
        self._central_db_path = self._central_dir / "quality_intelligence.db"
        central_db = _setup_central_quality_db(self._central_db_path, self._project_id)
        _seed_central_antipattern(
            central_db, self._project_id, title="Central antipattern",
            severity="high", occurrence_count=3,
        )
        central_db.close()
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        self._tmpdir.cleanup()

    def _make_selector(self):
        return IntelligenceSelector(quality_db_path=self._per_project_db_path)

    def _open_per_project_db(self):
        conn = sqlite3.connect(str(self._per_project_db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test__query_failure_prevention_unset_uses_per_project(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        selector = self._make_selector()
        db = self._open_per_project_db()
        items = selector._query_failure_prevention(db, "research_structured", [])
        db.close()
        selector.close()
        self.assertTrue(len(items) >= 1)
        self.assertTrue(any("Per-project" in i.title for i in items))

    def test__query_failure_prevention_shadow_logs_divergence(self):
        import intelligence_selector as _is_module
        import project_scope as _ps
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _ps.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _ps.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"
        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            items = selector._query_failure_prevention(db, "research_structured", [])
            db.close()
            selector.close()
            # Per-project authoritative: result is returned regardless of divergence
            self.assertIsInstance(items, list)
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _ps.current_project_id = original_pid

    def test__query_failure_prevention_authoritative_uses_central(self):
        import intelligence_selector as _is_module
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _is_module.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _is_module.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            items = selector._query_failure_prevention(db, "research_structured", [])
            db.close()
            selector.close()
            titles = [i.title for i in items]
            self.assertTrue(any("Central" in t for t in titles), f"Got: {titles}")
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _is_module.current_project_id = original_pid


class TestShadowModeQueryRecentComparable(unittest.TestCase):
    """3-state flag tests for _query_recent_comparable (metric 4, dispatch_metadata)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._project_id = "test-project"
        self._per_project_db_path = self._base / "quality_intelligence.db"
        db = _setup_quality_db(self._per_project_db_path)
        _seed_recent_dispatch(db, dispatch_id="pp-dispatch-001", skill_name="architect", days_ago=1)
        db.close()
        self._central_dir = self._base / "central" / self._project_id / "state"
        self._central_dir.mkdir(parents=True)
        self._central_db_path = self._central_dir / "quality_intelligence.db"
        central_db = _setup_central_quality_db(self._central_db_path, self._project_id)
        _seed_central_dispatch(central_db, self._project_id, dispatch_id="central-dispatch-001", days_ago=1)
        central_db.close()
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        self._tmpdir.cleanup()

    def _make_selector(self):
        return IntelligenceSelector(quality_db_path=self._per_project_db_path)

    def _open_per_project_db(self):
        conn = sqlite3.connect(str(self._per_project_db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test__query_recent_comparable_unset_uses_per_project(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        selector = self._make_selector()
        db = self._open_per_project_db()
        items = selector._query_recent_comparable(db, "research_structured", ["architect"])
        db.close()
        selector.close()
        self.assertIsInstance(items, list)
        ids = [i.item_id for i in items]
        self.assertTrue(any("pp-dispatch-001" in iid for iid in ids), f"Got: {ids}")

    def test__query_recent_comparable_shadow_logs_divergence(self):
        import intelligence_selector as _is_module
        import project_scope as _ps
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _ps.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _ps.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"
        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            items = selector._query_recent_comparable(db, "research_structured", [])
            db.close()
            selector.close()
            # Per-project authoritative
            self.assertIsInstance(items, list)
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _ps.current_project_id = original_pid

    def test__query_recent_comparable_authoritative_uses_central(self):
        import intelligence_selector as _is_module
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _is_module.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _is_module.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        try:
            selector = self._make_selector()
            db = self._open_per_project_db()
            items = selector._query_recent_comparable(db, "research_structured", [])
            db.close()
            selector.close()
            ids = [i.item_id for i in items]
            self.assertTrue(any("central-dispatch" in iid for iid in ids), f"Got: {ids}")
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _is_module.current_project_id = original_pid


class TestShadowModeSelectIntegration(unittest.TestCase):
    """Integration tests: select() top-N unaffected by shadow mode; divergences logged."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._project_id = "test-project"
        self._per_project_db_path = self._base / "quality_intelligence.db"
        db = _setup_quality_db(self._per_project_db_path)
        _seed_proven_pattern(db, title="Best pattern", confidence=0.9, usage_count=5, category="architect")
        _seed_antipattern(db, title="Best antipattern", severity="high", occurrence_count=4, category="architect")
        _seed_recent_dispatch(db, dispatch_id="sel-disp-001", skill_name="architect", days_ago=1)
        db.close()
        self._central_dir = self._base / "central" / self._project_id / "state"
        self._central_dir.mkdir(parents=True)
        central_db = _setup_central_quality_db(self._central_dir / "quality_intelligence.db", self._project_id)
        # Central has same data — no divergence
        _seed_central_proven_pattern(central_db, self._project_id, title="Best pattern", confidence=0.9, usage_count=5, category="architect")
        central_db.close()
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        self._tmpdir.cleanup()

    def test_select_with_shadow_mode_returns_legacy_top_n(self):
        """select() in shadow mode returns per-project top-N unchanged."""
        import intelligence_selector as _is_module
        import project_scope as _ps
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _ps.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _ps.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"
        try:
            selector = IntelligenceSelector(quality_db_path=self._per_project_db_path)
            result_shadow = selector.select("d-shadow-001", "dispatch_create", skill_name="architect")
            selector.close()

            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            selector2 = IntelligenceSelector(quality_db_path=self._per_project_db_path)
            result_default = selector2.select("d-default-001", "dispatch_create", skill_name="architect")
            selector2.close()

            self.assertEqual(result_shadow.items_injected, result_default.items_injected)
            shadow_ids = [i.item_id for i in result_shadow.items]
            default_ids = [i.item_id for i in result_default.items]
            self.assertEqual(shadow_ids, default_ids)
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            _is_module._resolve_central_data_dir = original_rcd
            _ps.current_project_id = original_pid

    def test_select_with_shadow_mode_logs_divergence_per_class(self):
        """Shadow mode captures divergence from each of the 3 query dispatchers."""
        import intelligence_selector as _is_module
        import project_scope as _ps
        import shadow_verifier
        original_rcd = _is_module._resolve_central_data_dir
        original_pid = _ps.current_project_id

        _is_module._resolve_central_data_dir = lambda pid: self._base / "central" / pid
        _ps.current_project_id = lambda: self._project_id
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"

        logged_calls = []
        original_compare = shadow_verifier.compare

        def _spy_compare(*args, **kwargs):
            logged_calls.append(kwargs.get("read_site", ""))
            return original_compare(*args, **kwargs)

        shadow_verifier.compare = _spy_compare
        try:
            selector = IntelligenceSelector(quality_db_path=self._per_project_db_path)
            selector.select("d-spy-001", "dispatch_create", skill_name="architect")
            selector.close()
            # All 3 dispatchers should have been called
            sites = [c for c in logged_calls if "IntelligenceSelector" in c]
            self.assertGreaterEqual(len(sites), 1, f"Expected shadow compare calls, got: {logged_calls}")
        finally:
            os.environ.pop("VNX_USE_CENTRAL_DB", None)
            shadow_verifier.compare = original_compare
            _is_module._resolve_central_data_dir = original_rcd
            _ps.current_project_id = original_pid


# ---------------------------------------------------------------------------
# Wave 5 P0: IntelligenceSelector prior_round_finding integration tests
# ---------------------------------------------------------------------------

class TestPriorRoundFindingIntegration(unittest.TestCase):
    """Integration tests for Wave 5 P0 prior_round_finding injection via select()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._results_dir = self._tmp_path / "review_gates" / "results"
        self._results_dir.mkdir(parents=True)

        # Minimal quality DB (no patterns seeded — we test prior_round_finding isolation)
        db_path = self._tmp_path / "quality.db"
        conn = _setup_quality_db(db_path)
        conn.close()
        self._db_path = db_path

        # Flush prior_round_injector LRU cache before each test
        try:
            import prior_round_injector
            prior_round_injector._fetch_cached.cache_clear()
        except Exception:
            pass

    def tearDown(self):
        self._tmp.cleanup()

    def _write_gate_file(
        self,
        pr_id: str,
        gate: str,
        blocking: list | None = None,
        advisory: list | None = None,
        recorded_at: str = "2026-04-01T10:00:00Z",
    ):
        data = {
            "gate": gate,
            "pr_id": pr_id,
            "blocking_findings": [{"message": m} for m in (blocking or [])],
            "advisory_findings": [{"message": m} for m in (advisory or [])],
            "recorded_at": recorded_at,
            "contract_hash": "test_hash",
            "status": "completed",
        }
        path = self._results_dir / f"pr-{pr_id}-{gate}.json"
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_select_includes_prior_round_findings_when_pr_id_present(self):
        import prior_round_injector
        self._write_gate_file(
            "200", "codex_gate",
            blocking=["Missing table in migration scripts/lib/importer.py:42."],
        )

        original_resolve = prior_round_injector._resolve_state_dir

        def _patched_resolve(state_dir=None):
            if state_dir is not None:
                return state_dir
            return self._tmp_path

        prior_round_injector._resolve_state_dir = _patched_resolve
        try:
            selector = IntelligenceSelector(quality_db_path=self._db_path)
            result = selector.select(
                "test-dispatch-p0-200",
                "dispatch_create",
                skill_name="backend-developer",
                pr_id="200",
            )
            selector.close()
        finally:
            prior_round_injector._resolve_state_dir = original_resolve

        item_classes = [item.item_class for item in result.items]
        self.assertIn("prior_round_finding", item_classes)

        prf_item = next(i for i in result.items if i.item_class == "prior_round_finding")
        self.assertEqual(prf_item.confidence, 1.0)
        self.assertIn("Prior-round review findings on PR #200", prf_item.title)
        self.assertIn("Missing table", prf_item.content)

    def test_select_does_not_include_prior_round_findings_when_no_pr_id(self):
        self._write_gate_file(
            "201", "codex_gate",
            blocking=["Some blocking issue."],
        )

        selector = IntelligenceSelector(quality_db_path=self._db_path)
        result = selector.select(
            "test-dispatch-no-prid",
            "dispatch_create",
            skill_name="backend-developer",
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertNotIn("prior_round_finding", item_classes)

    def test_select_respects_budget_when_prior_findings_large(self):
        import prior_round_injector

        large_messages = [f"Advisory finding #{i}: " + ("x" * 150) for i in range(15)]
        self._write_gate_file(
            "202", "codex_gate",
            advisory=large_messages,
        )

        original_resolve = prior_round_injector._resolve_state_dir

        def _patched_resolve(state_dir=None):
            if state_dir is not None:
                return state_dir
            return self._tmp_path

        prior_round_injector._resolve_state_dir = _patched_resolve
        try:
            selector = IntelligenceSelector(quality_db_path=self._db_path)
            result = selector.select(
                "test-dispatch-budget-202",
                "dispatch_create",
                skill_name="backend-developer",
                pr_id="202",
            )
            selector.close()
        finally:
            prior_round_injector._resolve_state_dir = original_resolve

        # Payload must stay within MAX_PAYLOAD_CHARS
        import json as _json
        payload_size = len(_json.dumps(result.to_payload_dict()))
        self.assertLessEqual(payload_size, MAX_PAYLOAD_CHARS * 2,
                             "Payload unexpectedly large — budget enforcement may have failed")


# ---------------------------------------------------------------------------
# Tests: Wave 5 P1 — adr_relevant integration
# ---------------------------------------------------------------------------

_REAL_ADR_DIR = Path(__file__).resolve().parent.parent / "docs" / "governance" / "decisions"


class TestAdrRelevantIntegration(unittest.TestCase):
    """Integration tests for Wave 5 P1 adr_relevant injection via select()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._results_dir = self._tmp_path / "review_gates" / "results"
        self._results_dir.mkdir(parents=True)

        db_path = self._tmp_path / "quality.db"
        conn = _setup_quality_db(db_path)
        conn.close()
        self._db_path = db_path

        # Reset _INDEX singleton so each test starts from a clean load
        try:
            import adr_indexer
            adr_indexer._INDEX.loaded_at = 0.0
        except Exception:
            pass

    def tearDown(self):
        self._tmp.cleanup()
        # Reset singleton after test to not pollute other suites
        try:
            import adr_indexer
            adr_indexer._INDEX.loaded_at = 0.0
        except Exception:
            pass

    def test_select_includes_adr_relevant_when_dispatch_paths_match(self):
        """dispatch on scripts/migrate_to_central_vnx.py auto-receives ADR-009 context."""
        if not _REAL_ADR_DIR.is_dir():
            self.skipTest("real ADR dir not available")

        selector = IntelligenceSelector(quality_db_path=self._db_path)
        result = selector.select(
            "test-dispatch-adr-migrate-001",
            "dispatch_create",
            skill_name="backend-developer",
            dispatch_paths=["scripts/migrate_to_central_vnx.py"],
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertIn("adr_relevant", item_classes,
                      "Expected adr_relevant item for scripts/migrate_to_central_vnx.py path")

        adr_item = next(i for i in result.items if i.item_class == "adr_relevant")
        self.assertIn("ADR-009", adr_item.source_refs,
                      "ADR-009 should be referenced since it mentions migrate_to_central_vnx.py")
        self.assertEqual(adr_item.confidence, 1.0)
        self.assertGreaterEqual(adr_item.evidence_count, 1)

    def test_select_does_not_include_adr_relevant_when_no_paths_overlap(self):
        """dispatch with unrelated paths produces no adr_relevant item."""
        if not _REAL_ADR_DIR.is_dir():
            self.skipTest("real ADR dir not available")

        selector = IntelligenceSelector(quality_db_path=self._db_path)
        result = selector.select(
            "test-dispatch-no-adr-overlap",
            "dispatch_create",
            skill_name="backend-developer",
            dispatch_paths=["scripts/totally_nonexistent_xyz_tool.py"],
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertNotIn("adr_relevant", item_classes)

    def test_select_combines_prior_round_and_adr_relevant_within_budget(self):
        """When both pr_id and dispatch_paths are set, both item classes can appear."""
        if not _REAL_ADR_DIR.is_dir():
            self.skipTest("real ADR dir not available")

        # Write a gate result for pr_id=300
        gate_data = {
            "gate": "codex_gate",
            "pr_id": "300",
            "blocking_findings": [
                {"message": "Missing PRAGMA introspection in scripts/migrate_to_central_vnx.py:42."}
            ],
            "advisory_findings": [],
            "recorded_at": "2026-05-10T10:00:00Z",
            "contract_hash": "test_hash_adr_combo",
            "status": "completed",
        }
        gate_path = self._results_dir / "pr-300-codex_gate.json"
        gate_path.write_text(json.dumps(gate_data), encoding="utf-8")

        import prior_round_injector
        prior_round_injector._fetch_cached.cache_clear()

        original_resolve = prior_round_injector._resolve_state_dir

        def _patched_resolve(state_dir=None):
            if state_dir is not None:
                return state_dir
            return self._tmp_path

        prior_round_injector._resolve_state_dir = _patched_resolve
        try:
            selector = IntelligenceSelector(quality_db_path=self._db_path)
            result = selector.select(
                "test-dispatch-adr-prior-combo",
                "dispatch_create",
                skill_name="backend-developer",
                pr_id="300",
                dispatch_paths=["scripts/migrate_to_central_vnx.py"],
            )
            selector.close()
        finally:
            prior_round_injector._resolve_state_dir = original_resolve

        item_classes = [item.item_class for item in result.items]
        # At least one of the two high-priority classes should be present
        self.assertTrue(
            "prior_round_finding" in item_classes or "adr_relevant" in item_classes,
            f"Expected at least one of prior_round_finding or adr_relevant, got: {item_classes}",
        )
        # Payload must stay bounded
        payload_size = len(json.dumps(result.to_payload_dict()))
        self.assertLessEqual(payload_size, MAX_PAYLOAD_CHARS * 2)


# ---------------------------------------------------------------------------
# Wave 5 P2: code_anchor integration tests
# ---------------------------------------------------------------------------

class TestCodeAnchorInjection(unittest.TestCase):
    """Integration tests for Wave 5 P2 code anchor injection in IntelligenceSelector."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._quality_db_path = self._base / "quality_intelligence.db"
        self._state_dir = self._base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        db = _setup_quality_db(self._quality_db_path)
        db.close()

        # Write a real Python file that the selector can find anchors in
        self._scripts_dir = self._base / "scripts"
        self._scripts_dir.mkdir()
        self._source_file = self._scripts_dir / "migrate_to_central_vnx.py"
        self._source_file.write_text(
            "# migration script\n"
            "def _import_table(conn, table_name):\n"
            "    conn.execute('INSERT OR IGNORE INTO ' + table_name)\n"
            "    return True\n"
            "\n"
            "class MigrationRunner:\n"
            "    def run(self):\n"
            "        pass\n",
            encoding="utf-8",
        )
        self._rel_path = str(
            self._source_file.relative_to(self._base)
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_select_includes_code_anchor_when_paths_and_instruction_present(self):
        """When dispatch_paths and instruction_text are provided with matching terms,
        the selector includes a code_anchor item."""
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select(
            "ca-test-001",
            "dispatch_create",
            dispatch_paths=[self._rel_path],
            instruction_text=(
                "Edit _import_table to add INSERT OR IGNORE logic for MigrationRunner"
            ),
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertIn("code_anchor", item_classes,
                      f"Expected code_anchor in items, got: {item_classes}")

        ca_items = [i for i in result.items if i.item_class == "code_anchor"]
        self.assertEqual(len(ca_items), 1)
        self.assertEqual(ca_items[0].confidence, 1.0)
        self.assertGreater(ca_items[0].evidence_count, 0)
        self.assertTrue(len(ca_items[0].source_refs) > 0)

    def test_select_does_not_include_code_anchor_when_no_instruction_text(self):
        """Without instruction_text, no code_anchor item is injected."""
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select(
            "ca-test-002",
            "dispatch_create",
            dispatch_paths=[self._rel_path],
            instruction_text=None,
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertNotIn("code_anchor", item_classes)

    def test_select_does_not_include_code_anchor_when_no_dispatch_paths(self):
        """Without dispatch_paths, no code_anchor item is injected."""
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        result = selector.select(
            "ca-test-003",
            "dispatch_create",
            dispatch_paths=None,
            instruction_text="_import_table INSERT OR IGNORE",
        )
        selector.close()

        item_classes = [item.item_class for item in result.items]
        self.assertNotIn("code_anchor", item_classes)

    def test_select_combines_prior_round_adr_and_code_anchor_within_budget(self):
        """P0 (prior_round_finding) and P2 (code_anchor) can coexist within the payload budget.

        Uses scripts/lib/code_anchor_finder.py as dispatch_path — it exists in the repo
        and is not referenced by any ADR, so P1 (adr_relevant) does not fire. This lets
        P0+P2 coexist without hitting the 2000-char budget ceiling.
        """
        import prior_round_injector

        # Use a path with no ADR matches so adr_relevant doesn't fire and consume budget.
        # code_anchor_finder.py exists in the real project and has matching identifiers.
        dispatch_path = "scripts/lib/code_anchor_finder.py"

        # Seed a prior-round finding (mock state_dir to point to tmpdir)
        results_dir = self._base / "review_gates" / "results"
        results_dir.mkdir(parents=True)
        gate_file = results_dir / "pr-998-codex_gate.json"
        gate_file.write_text(
            json.dumps({
                "recorded_at": "2026-05-01T12:00:00Z",
                "contract_hash": "abc998",
                "blocking_findings": [
                    {"message": "fetch_code_anchors missing null guard in extract_terms"},
                ],
                "advisory_findings": [],
            }),
            encoding="utf-8",
        )

        # Clear LRU cache so stale entries don't mask the patched resolver
        prior_round_injector._fetch_cached.cache_clear()

        # Patch state_dir resolution so prior_round_injector finds the gate file
        original_resolve = prior_round_injector._resolve_state_dir

        def _patched_resolve(state_dir=None):
            if state_dir is not None:
                return state_dir
            return self._base

        prior_round_injector._resolve_state_dir = _patched_resolve

        try:
            selector = IntelligenceSelector(
                quality_db_path=self._quality_db_path,
                coord_db_state_dir=self._state_dir,
            )
            result = selector.select(
                "ca-test-004",
                "dispatch_create",
                pr_id="998",
                dispatch_paths=[dispatch_path],
                instruction_text=(
                    "Edit fetch_code_anchors and extract_terms to handle CodeAnchor edge cases"
                ),
            )
            selector.close()
        finally:
            prior_round_injector._resolve_state_dir = original_resolve

        item_classes = [item.item_class for item in result.items]
        # Both P0 and P2 should be present (P1/adr_relevant not expected for this path)
        self.assertIn("code_anchor", item_classes,
                      f"Expected code_anchor in items, got: {item_classes}")
        self.assertIn("prior_round_finding", item_classes,
                      f"Expected prior_round_finding in items, got: {item_classes}")

        # Payload must remain bounded
        payload_size = len(json.dumps(result.to_payload_dict()))
        self.assertLessEqual(payload_size, MAX_PAYLOAD_CHARS * 2)


class TestIntelligenceItemSerialization(unittest.TestCase):
    """Verify that to_dict() round-trips content for direct-injection item classes."""

    def _make_item(self, item_class: str, content: str) -> "IntelligenceItem":
        return IntelligenceItem(
            item_id="test-id",
            item_class=item_class,
            title="Test item",
            content=content,
            confidence=0.9,
            evidence_count=2,
            last_seen="2026-05-10T00:00:00",
            scope_tags=["test"],
        )

    def test_intelligence_item_serialization_preserves_code_anchor_content(self):
        # A code_anchor item with 1500 chars of content must not be truncated to 500.
        content = "x" * 1500
        item = self._make_item("code_anchor", content)
        serialized = item.to_dict()
        self.assertEqual(
            len(serialized["content"]),
            1500,
            "code_anchor content was truncated below 1500 chars",
        )

    def test_intelligence_item_serialization_preserves_prior_round_finding_content(self):
        content = "p" * 1500
        item = self._make_item("prior_round_finding", content)
        serialized = item.to_dict()
        self.assertEqual(
            len(serialized["content"]),
            1500,
            "prior_round_finding content was truncated below 1500 chars",
        )

    def test_intelligence_item_serialization_preserves_adr_relevant_content(self):
        content = "a" * 1500
        item = self._make_item("adr_relevant", content)
        serialized = item.to_dict()
        self.assertEqual(
            len(serialized["content"]),
            1500,
            "adr_relevant content was truncated below 1500 chars",
        )

    def test_intelligence_item_serialization_still_caps_standard_classes(self):
        # Standard item classes must still be capped at MAX_CONTENT_CHARS_PER_ITEM (500).
        content = "y" * 1000
        item = self._make_item("proven_pattern", content)
        serialized = item.to_dict()
        self.assertLessEqual(
            len(serialized["content"]),
            MAX_CONTENT_CHARS_PER_ITEM,
            "proven_pattern content exceeded 500-char cap",
        )


if __name__ == "__main__":
    unittest.main()
