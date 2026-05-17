#!/usr/bin/env python3
"""
Regression tests for exception-handling hardening in intelligence_selector.py (OI-1437).

Updated for the per-source module split: internal query methods moved to
intelligence_sources submodules, so tests now reach them directly.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_selector import (
    IntelligenceItem,
    IntelligenceSelector,
    InjectionResult,
)


def _make_selector(tmp: str) -> IntelligenceSelector:
    state_dir = Path(tmp) / "coord"
    state_dir.mkdir(exist_ok=True)
    quality_db = Path(tmp) / "quality.db"
    return IntelligenceSelector(
        quality_db_path=quality_db,
        coord_db_state_dir=state_dir,
    )


def _make_item(item_id: str = "test-item") -> IntelligenceItem:
    return IntelligenceItem(
        item_id=item_id,
        item_class="proven_pattern",
        title="Test pattern",
        content="Test content",
        confidence=0.8,
        evidence_count=3,
        last_seen="2026-01-01T00:00:00",
        scope_tags=["test"],
        task_class_filter=[],
    )


def _make_result(dispatch_id: str = "test-dispatch-001") -> InjectionResult:
    return InjectionResult(
        dispatch_id=dispatch_id,
        injection_point="dispatch_create",
        injected_at="2026-05-14T00:00:00Z",
        task_class="backend-developer",
        items=[_make_item()],
        suppressed=[],
    )


def _broken_central_conn() -> MagicMock:
    conn = MagicMock(spec=sqlite3.Connection)
    conn.execute.side_effect = sqlite3.OperationalError("no such table: success_patterns")
    conn.close = MagicMock()
    return conn


class TestRunsCleanInDefaultEnv(unittest.TestCase):
    """Selector constructs and select() runs without unhandled exceptions."""

    def test_construct_selector_no_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            self.assertIsNotNone(sel)

    def test_select_returns_result_without_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            result = sel.select(
                dispatch_id="clean-env-test",
                injection_point="dispatch_create",
                task_class="backend-developer",
                scope_tags=[],
            )
            self.assertIsNotNone(result)
            self.assertIsInstance(result.items, list)


class TestCorruptStateLogsWarning(unittest.TestCase):
    """record_injection with a broken DB logs instead of crashing."""

    def test_corrupt_db_logs_warning(self) -> None:
        """sqlite3.Error during injection audit write surfaces as log.warning."""
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            result = _make_result()

            import runtime_coordination as _rc
            with (
                patch.object(_rc, "get_connection", side_effect=sqlite3.OperationalError("db locked")),
                self.assertLogs("intelligence_sources._recording", level="WARNING") as cm,
            ):
                sel.record_injection(result)

            logged = "\n".join(cm.output)
            self.assertIn("Failed to record injection audit", logged)

    def test_stamp_source_ids_db_error_logs_debug(self) -> None:
        """sqlite3.Error in _stamp is caught and logged at DEBUG by intelligence_selector."""
        from contextlib import contextmanager

        @contextmanager
        def _noop_conn(_state_dir):
            conn = MagicMock(spec=sqlite3.Connection)
            conn.execute.return_value = MagicMock()
            conn.commit.return_value = None
            yield conn

        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            result = _make_result()

            import runtime_coordination as _rc
            import intelligence_selector as _is_mod
            with (
                patch.object(_rc, "get_connection", side_effect=_noop_conn),
                patch.object(_is_mod, "_stamp", side_effect=sqlite3.OperationalError("locked")),
                self.assertLogs("intelligence_selector", level="DEBUG") as cm,
            ):
                sel.record_injection(result)

            logged = "\n".join(cm.output)
            self.assertIn("source_dispatch_ids", logged)


class TestCentralDbQueryExceptionSilenced(unittest.TestCase):
    """Central DB query functions catch sqlite3.Error and return empty list."""

    def test_proven_patterns_central_db_error_returns_empty(self) -> None:
        from intelligence_sources.proven_pattern import _query_central
        with self.assertLogs("intelligence_sources.proven_pattern", level="DEBUG") as cm:
            items = _query_central(
                "backend-developer", [],
                lambda: _broken_central_conn(),
                project_id_fn=lambda: None,
            )
        self.assertEqual(items, [])
        self.assertTrue(any("proven-patterns" in line for line in cm.output))

    def test_failure_prevention_central_db_error_returns_empty(self) -> None:
        from intelligence_sources.failure_prevention import _query_central
        with self.assertLogs("intelligence_sources.failure_prevention", level="DEBUG") as cm:
            items = _query_central(
                "backend-developer", [],
                lambda: _broken_central_conn(),
                project_id_fn=lambda: None,
            )
        self.assertEqual(items, [])
        self.assertTrue(any("failure-prevention" in line for line in cm.output))

    def test_recent_comparable_central_db_error_returns_empty(self) -> None:
        from intelligence_sources.recent_comparable import _query_central
        with self.assertLogs("intelligence_sources.recent_comparable", level="DEBUG") as cm:
            items = _query_central(
                "backend-developer", [],
                lambda: _broken_central_conn(),
                project_id_fn=lambda: None,
            )
        self.assertEqual(items, [])
        self.assertTrue(any("recent-comparable" in line for line in cm.output))


class TestShadowVerifierExceptionSilenced(unittest.TestCase):
    """Shadow compare exceptions are caught and logged at DEBUG, not raised."""

    def _make_per_project_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, description TEXT, category TEXT,
                confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
                source_dispatch_ids TEXT, first_seen DATETIME, last_used DATETIME,
                valid_until DATETIME DEFAULT NULL
            );
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
                role TEXT, skill_name TEXT, gate TEXT,
                outcome_status TEXT, dispatched_at DATETIME,
                pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0
            );
            CREATE TABLE antipatterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, description TEXT, category TEXT,
                severity TEXT DEFAULT 'medium', occurrence_count INTEGER DEFAULT 0,
                why_problematic TEXT, better_alternative TEXT,
                first_seen DATETIME, last_seen DATETIME,
                valid_until DATETIME DEFAULT NULL
            );
        """)
        return db

    def test_shadow_proven_patterns_exception_logged(self) -> None:
        import intelligence_sources.proven_pattern as _mod
        db = self._make_per_project_db()
        broken_verifier = MagicMock()
        broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
        with (
            patch.object(_mod, "_shadow_verifier", broken_verifier),
            patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
            self.assertLogs("intelligence_sources.proven_pattern", level="DEBUG") as cm,
        ):
            result = _mod.query_proven_patterns(
                db, "backend-developer", [],
                has_column_fn=lambda t, c: False,
                central_conn_fn=lambda: None,
                reconcile_fn=lambda: None,
            )
        db.close()
        self.assertEqual(result, [])
        self.assertTrue(any("Shadow compare" in line for line in cm.output))

    def test_shadow_failure_prevention_exception_logged(self) -> None:
        import intelligence_sources.failure_prevention as _mod
        db = self._make_per_project_db()
        broken_verifier = MagicMock()
        broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
        with (
            patch.object(_mod, "_shadow_verifier", broken_verifier),
            patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
            self.assertLogs("intelligence_sources.failure_prevention", level="DEBUG") as cm,
        ):
            result = _mod.query_failure_prevention(
                db, "backend-developer", [],
                has_column_fn=lambda t, c: False,
                central_conn_fn=lambda: None,
            )
        db.close()
        self.assertEqual(result, [])
        self.assertTrue(any("Shadow compare" in line for line in cm.output))

    def test_shadow_recent_comparable_exception_logged(self) -> None:
        import intelligence_sources.recent_comparable as _mod
        db = self._make_per_project_db()
        broken_verifier = MagicMock()
        broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
        with (
            patch.object(_mod, "_shadow_verifier", broken_verifier),
            patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
            self.assertLogs("intelligence_sources.recent_comparable", level="DEBUG") as cm,
        ):
            result = _mod.query_recent_comparable(
                db, "backend-developer", [],
                has_column_fn=lambda t, c: False,
                central_conn_fn=lambda: None,
            )
        db.close()
        self.assertEqual(result, [])
        self.assertTrue(any("Shadow compare" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
