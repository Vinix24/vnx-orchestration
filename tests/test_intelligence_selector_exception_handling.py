#!/usr/bin/env python3
"""
Regression tests for exception-handling hardening in intelligence_selector.py (OI-1437).

Covers the 10 silent-except sites narrowed in the cleanup PR: every broad
``except Exception: pass`` is now narrowed and logged. These tests verify
that (a) the selector runs cleanly in a default env, and (b) DB errors surface
as log messages instead of being swallowed silently.
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
    """record_injection with a broken DB logs a warning instead of crashing."""

    def test_corrupt_db_logs_warning(self) -> None:
        """sqlite3.Error during injection audit write surfaces as log.warning."""
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            result = _make_result()

            # Patch runtime_coordination.get_connection to raise sqlite3.Error
            import runtime_coordination as _rc
            with (
                patch.object(_rc, "get_connection", side_effect=sqlite3.OperationalError("db locked")),
                self.assertLogs("intelligence_selector", level="WARNING") as cm,
            ):
                sel.record_injection(result)

            logged = "\n".join(cm.output)
            self.assertIn("Failed to record injection audit", logged)

    def test_stamp_source_ids_db_error_logs_debug(self) -> None:
        """sqlite3.Error in stamp_source_dispatch_ids is caught and logged at DEBUG."""
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
            with (
                patch.object(_rc, "get_connection", side_effect=_noop_conn),
                patch.object(sel, "stamp_source_dispatch_ids", side_effect=sqlite3.OperationalError("locked")),
                self.assertLogs("intelligence_selector", level="DEBUG") as cm,
            ):
                sel.record_injection(result)

            logged = "\n".join(cm.output)
            self.assertIn("source_dispatch_ids", logged)


class TestCentralDbQueryExceptionSilenced(unittest.TestCase):
    """Central DB query methods catch sqlite3.Error and return empty list."""

    def _broken_conn(self) -> MagicMock:
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("no such table: success_patterns")
        conn.close = MagicMock()
        return conn

    def test_proven_patterns_central_db_error_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            with patch.object(sel, "_get_central_qi_conn", return_value=self._broken_conn()):
                with self.assertLogs("intelligence_selector", level="DEBUG") as cm:
                    items = sel._query_proven_patterns_central("backend-developer", [])
            self.assertEqual(items, [])
            self.assertTrue(any("proven-patterns" in line for line in cm.output))

    def test_failure_prevention_central_db_error_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            with patch.object(sel, "_get_central_qi_conn", return_value=self._broken_conn()):
                with self.assertLogs("intelligence_selector", level="DEBUG") as cm:
                    items = sel._query_failure_prevention_central("backend-developer", [])
            self.assertEqual(items, [])
            self.assertTrue(any("failure-prevention" in line for line in cm.output))

    def test_recent_comparable_central_db_error_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            with patch.object(sel, "_get_central_qi_conn", return_value=self._broken_conn()):
                with self.assertLogs("intelligence_selector", level="DEBUG") as cm:
                    items = sel._query_recent_comparable_central("backend-developer", [])
            self.assertEqual(items, [])
            self.assertTrue(any("recent-comparable" in line for line in cm.output))


class TestShadowVerifierExceptionSilenced(unittest.TestCase):
    """Shadow compare exceptions are caught and logged at DEBUG, not raised."""

    def test_shadow_proven_patterns_exception_logged(self) -> None:
        import intelligence_selector as _mod
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            broken_verifier = MagicMock()
            broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
            quality_db = sqlite3.connect(":memory:")
            with (
                patch.object(_mod, "_shadow_verifier", broken_verifier),
                patch.object(sel, "_query_proven_patterns_per_project", return_value=[]),
                patch.object(sel, "_query_proven_patterns_central", return_value=[]),
                patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
                self.assertLogs("intelligence_selector", level="DEBUG") as cm,
            ):
                result = sel._query_proven_patterns(quality_db, "backend-developer", [])
            self.assertEqual(result, [])
            self.assertTrue(any("Shadow compare" in line for line in cm.output))

    def test_shadow_failure_prevention_exception_logged(self) -> None:
        import intelligence_selector as _mod
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            broken_verifier = MagicMock()
            broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
            quality_db = sqlite3.connect(":memory:")
            with (
                patch.object(_mod, "_shadow_verifier", broken_verifier),
                patch.object(sel, "_query_failure_prevention_per_project", return_value=[]),
                patch.object(sel, "_query_failure_prevention_central", return_value=[]),
                patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
                self.assertLogs("intelligence_selector", level="DEBUG") as cm,
            ):
                result = sel._query_failure_prevention(quality_db, "backend-developer", [])
            self.assertEqual(result, [])
            self.assertTrue(any("Shadow compare" in line for line in cm.output))

    def test_shadow_recent_comparable_exception_logged(self) -> None:
        import intelligence_selector as _mod
        with tempfile.TemporaryDirectory() as tmp:
            sel = _make_selector(tmp)
            broken_verifier = MagicMock()
            broken_verifier.compare.side_effect = RuntimeError("shadow verifier exploded")
            quality_db = sqlite3.connect(":memory:")
            with (
                patch.object(_mod, "_shadow_verifier", broken_verifier),
                patch.object(sel, "_query_recent_comparable_per_project", return_value=[]),
                patch.object(sel, "_query_recent_comparable_central", return_value=[]),
                patch.dict("os.environ", {"VNX_USE_CENTRAL_DB": "shadow"}),
                self.assertLogs("intelligence_selector", level="DEBUG") as cm,
            ):
                result = sel._query_recent_comparable(quality_db, "backend-developer", [])
            self.assertEqual(result, [])
            self.assertTrue(any("Shadow compare" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
