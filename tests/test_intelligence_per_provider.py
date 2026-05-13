#!/usr/bin/env python3
"""
Tests for Wave 4.5 PR-3 redo: build_intelligence_context() per-provider with
empty-dispatch_id guard.

Covers:
- build_intelligence_context returns None (no audit write) for empty dispatch_id
- codex_adapter._build_prompt skips intelligence section when dispatch_id missing
- gemini_adapter._build_prompt skips intelligence section when dispatch_id missing
- Existing real-dispatch_id paths produce intelligence sections and write audit rows
- record_injection / emit_event raise ValueError on empty dispatch_id
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR / "lib" / "adapters"))

from intelligence_selector import (
    IntelligenceContext,
    IntelligenceSelector,
    InjectionResult,
    IntelligenceItem,
    SuppressionRecord,
    build_intelligence_context,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

def _setup_quality_db(db_path: Path) -> sqlite3.Connection:
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


def _setup_coord_db(state_dir: Path) -> None:
    """Initialize runtime_coordination.db in state_dir."""
    init_schema(str(state_dir))


def _seed_antipattern(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO antipatterns (title, description, category, severity,
           why_problematic, better_alternative, occurrence_count, last_seen,
           pattern_data, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)""",
        (
            "Test antipattern",
            "Test antipattern",
            "backend-developer",
            "high",
            "Bad practice causes failures.",
            "Use better approach.",
            3,
            "2026-03-27T09:00:00",
            "2026-03-27T09:00:00",
        ),
    )
    conn.commit()


def _make_injection_result(dispatch_id: str = "disp-1", items=None) -> InjectionResult:
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-05-13T00:00:00.000000Z",
        items=items or [],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id=dispatch_id,
    )


def _make_item(item_class: str = "failure_prevention") -> IntelligenceItem:
    return IntelligenceItem(
        item_id="item-1",
        item_class=item_class,
        title="Avoid bad pattern",
        content="This causes failures.",
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-05-01T00:00:00Z",
        scope_tags=[],
    )


# ---------------------------------------------------------------------------
# Tests: build_intelligence_context empty-dispatch_id guard
# ---------------------------------------------------------------------------

class TestBuildIntelligenceContextEmptyIdGuard(unittest.TestCase):
    """build_intelligence_context must return None and write zero audit rows when dispatch_id is empty."""

    def test_returns_none_for_empty_string(self):
        result = build_intelligence_context(dispatch_id="")
        self.assertIsNone(result)

    def test_returns_none_for_whitespace_only(self):
        result = build_intelligence_context(dispatch_id="   ")
        self.assertIsNone(result)

    def test_returns_none_for_none_coerced(self):
        result = build_intelligence_context(dispatch_id=(None or ""))
        self.assertIsNone(result)

    def test_no_audit_row_created_for_empty_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            quality_db = state_dir / "quality_intelligence.db"
            _setup_quality_db(quality_db)
            _setup_coord_db(state_dir)

            build_intelligence_context(
                dispatch_id="",
                quality_db_path=quality_db,
                coord_state_dir=state_dir,
            )

            with get_connection(str(state_dir)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM intelligence_injections WHERE dispatch_id=''"
                ).fetchone()
            self.assertEqual(row["cnt"], 0)

    def test_returns_context_for_real_dispatch_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            quality_db = state_dir / "quality_intelligence.db"
            q_conn = _setup_quality_db(quality_db)
            _setup_coord_db(state_dir)
            _seed_antipattern(q_conn)
            q_conn.close()

            ctx = build_intelligence_context(
                dispatch_id="test-dispatch-real",
                quality_db_path=quality_db,
                coord_state_dir=state_dir,
            )
            self.assertIsNotNone(ctx)
            self.assertIsInstance(ctx, IntelligenceContext)
            self.assertEqual(ctx.dispatch_id, "test-dispatch-real")


# ---------------------------------------------------------------------------
# Tests: IntelligenceContext.serialize_for per-provider format
# ---------------------------------------------------------------------------

class TestIntelligenceContextSerializeFor(unittest.TestCase):
    """serialize_for() returns provider-specific markdown or '' for no items."""

    def test_serialize_for_codex_compact_format(self):
        ctx = IntelligenceContext(
            result=_make_injection_result(items=[_make_item("failure_prevention")]),
            dispatch_id="disp-1",
        )
        markdown = ctx.serialize_for("codex")
        self.assertIn("Intelligence Context", markdown)
        self.assertIn("1.", markdown)
        self.assertNotIn("### Antipatterns", markdown)

    def test_serialize_for_gemini_full_markdown(self):
        ctx = IntelligenceContext(
            result=_make_injection_result(items=[_make_item("failure_prevention")]),
            dispatch_id="disp-1",
        )
        markdown = ctx.serialize_for("gemini")
        self.assertIn("### Antipatterns to avoid", markdown)

    def test_serialize_for_litellm_full_markdown(self):
        ctx = IntelligenceContext(
            result=_make_injection_result(items=[_make_item("proven_pattern")]),
            dispatch_id="disp-1",
        )
        markdown = ctx.serialize_for("litellm")
        self.assertIn("### Proven success patterns", markdown)

    def test_serialize_for_returns_empty_when_no_items(self):
        ctx = IntelligenceContext(
            result=_make_injection_result(items=[]),
            dispatch_id="disp-1",
        )
        self.assertEqual(ctx.serialize_for("codex"), "")
        self.assertEqual(ctx.serialize_for("gemini"), "")


# ---------------------------------------------------------------------------
# Tests: record_injection / emit_event guard
# ---------------------------------------------------------------------------

class TestAuditMethodGuards(unittest.TestCase):
    """record_injection and emit_event raise ValueError for empty dispatch_id.

    These guards fire before any DB access, so no coord DB is needed.
    """

    def test_record_injection_raises_for_empty_dispatch_id(self):
        selector = IntelligenceSelector()
        try:
            with self.assertRaises(ValueError) as ctx:
                selector.record_injection(_make_injection_result(dispatch_id=""))
            self.assertIn("dispatch_id", str(ctx.exception))
        finally:
            selector.close()

    def test_emit_event_raises_for_empty_dispatch_id(self):
        selector = IntelligenceSelector()
        try:
            with self.assertRaises(ValueError) as ctx:
                selector.emit_event(_make_injection_result(dispatch_id=""))
            self.assertIn("dispatch_id", str(ctx.exception))
        finally:
            selector.close()

    def test_record_injection_raises_for_whitespace_dispatch_id(self):
        selector = IntelligenceSelector()
        try:
            with self.assertRaises(ValueError):
                selector.record_injection(_make_injection_result(dispatch_id="   "))
        finally:
            selector.close()

    def test_emit_event_raises_for_whitespace_dispatch_id(self):
        selector = IntelligenceSelector()
        try:
            with self.assertRaises(ValueError):
                selector.emit_event(_make_injection_result(dispatch_id="   "))
        finally:
            selector.close()


# ---------------------------------------------------------------------------
# Tests: codex_adapter skips intelligence when no dispatch_id
# ---------------------------------------------------------------------------

class TestCodexAdapterSkipsIntelligence(unittest.TestCase):
    """CodexAdapter._build_prompt produces no intelligence section when dispatch_id absent."""

    def setUp(self):
        self._patch_collect = patch(
            "codex_adapter.collect_file_contents", return_value=""
        )
        self._patch_collect.start()

    def tearDown(self):
        self._patch_collect.stop()

    def _get_adapter(self):
        from codex_adapter import CodexAdapter
        return CodexAdapter(terminal_id="T1")

    def test_no_intelligence_section_without_dispatch_id(self):
        adapter = self._get_adapter()
        prompt = adapter._build_prompt(
            "Review this code.",
            changed_files=[],
            role=None,
            dispatch_metadata=None,
        )
        self.assertNotIn("Intelligence Context", prompt)
        self.assertNotIn("Antipatterns", prompt)

    def test_no_intelligence_section_with_empty_dispatch_id(self):
        adapter = self._get_adapter()
        prompt = adapter._build_prompt(
            "Review this code.",
            changed_files=[],
            role=None,
            dispatch_metadata={"dispatch_id": ""},
        )
        self.assertNotIn("Intelligence Context", prompt)

    def test_no_error_raised_when_dispatch_id_absent(self):
        adapter = self._get_adapter()
        try:
            adapter._build_prompt(
                "Review.",
                changed_files=[],
                role=None,
                dispatch_metadata=None,
            )
        except Exception as exc:
            self.fail(f"_build_prompt raised unexpectedly: {exc}")

    def test_intelligence_section_present_with_real_dispatch_id(self):
        mock_item = _make_item("failure_prevention")
        mock_result = _make_injection_result(dispatch_id="real-dispatch-1", items=[mock_item])
        mock_ctx = IntelligenceContext(result=mock_result, dispatch_id="real-dispatch-1")

        # build_intelligence_context is imported lazily inside _build_prompt,
        # so patch at the source module level.
        with patch("intelligence_selector.build_intelligence_context", return_value=mock_ctx):
            adapter = self._get_adapter()
            prompt = adapter._build_prompt(
                "Review.",
                changed_files=[],
                role=None,
                dispatch_metadata={"dispatch_id": "real-dispatch-1"},
            )

        self.assertIn("Intelligence Context", prompt)


# ---------------------------------------------------------------------------
# Tests: gemini_adapter skips intelligence when no dispatch_id
# ---------------------------------------------------------------------------

class TestGeminiAdapterSkipsIntelligence(unittest.TestCase):
    """GeminiAdapter._build_prompt produces no intelligence section when dispatch_id absent."""

    def setUp(self):
        self._patch_collect = patch(
            "gemini_adapter.collect_file_contents", return_value=""
        )
        self._patch_collect.start()

    def tearDown(self):
        self._patch_collect.stop()

    def _get_adapter(self):
        from gemini_adapter import GeminiAdapter
        return GeminiAdapter(terminal_id="T3")

    def test_no_intelligence_section_without_dispatch_id(self):
        adapter = self._get_adapter()
        prompt = adapter._build_prompt(
            "Review this code.",
            changed_files=[],
            role=None,
            dispatch_metadata=None,
        )
        self.assertNotIn("Intelligence Context", prompt)
        self.assertNotIn("Antipatterns", prompt)

    def test_no_intelligence_section_with_empty_dispatch_id(self):
        adapter = self._get_adapter()
        prompt = adapter._build_prompt(
            "Review this code.",
            changed_files=[],
            role=None,
            dispatch_metadata={"dispatch_id": ""},
        )
        self.assertNotIn("Intelligence Context", prompt)

    def test_no_error_raised_when_dispatch_id_absent(self):
        adapter = self._get_adapter()
        try:
            adapter._build_prompt(
                "Review.",
                changed_files=[],
                role=None,
                dispatch_metadata=None,
            )
        except Exception as exc:
            self.fail(f"_build_prompt raised unexpectedly: {exc}")

    def test_intelligence_section_present_with_real_dispatch_id(self):
        mock_item = _make_item("failure_prevention")
        mock_result = _make_injection_result(dispatch_id="real-dispatch-2", items=[mock_item])
        mock_ctx = IntelligenceContext(result=mock_result, dispatch_id="real-dispatch-2")

        with patch("intelligence_selector.build_intelligence_context", return_value=mock_ctx):
            adapter = self._get_adapter()
            prompt = adapter._build_prompt(
                "Review.",
                changed_files=[],
                role=None,
                dispatch_metadata={"dispatch_id": "real-dispatch-2"},
            )

        self.assertIn("Antipatterns to avoid", prompt)


# ---------------------------------------------------------------------------
# Tests: no audit pollution — zero '' rows after adapter call without dispatch_id
# ---------------------------------------------------------------------------

class TestNoAuditPollution(unittest.TestCase):
    """After running build_intelligence_context without dispatch_id, no '' rows in intelligence_injections."""

    def test_no_empty_dispatch_id_rows_after_no_id_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            quality_db = state_dir / "quality_intelligence.db"
            _setup_quality_db(quality_db)
            _setup_coord_db(state_dir)

            build_intelligence_context(
                dispatch_id="",
                quality_db_path=quality_db,
                coord_state_dir=state_dir,
            )

            with get_connection(str(state_dir)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM intelligence_injections WHERE dispatch_id=''"
                ).fetchone()
            self.assertEqual(row["cnt"], 0)


if __name__ == "__main__":
    unittest.main()
