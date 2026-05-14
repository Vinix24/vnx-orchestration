#!/usr/bin/env python3
"""
Regression tests for exception-handling hardening in gather_intelligence.py (OI-1437).

Covers the 6 silent-except sites narrowed in the cleanup PR. Verifies that
(a) the gatherer runs cleanly with no DB present, and (b) DB errors surface
as debug log messages instead of being swallowed silently.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LIB_DIR))

import gather_intelligence as gi
from gather_intelligence import T0IntelligenceGatherer


def _make_gatherer() -> T0IntelligenceGatherer:
    """Construct a T0IntelligenceGatherer bypassing VNX env setup in __init__."""
    obj = object.__new__(T0IntelligenceGatherer)
    obj.quality_db = None
    obj.quality_db_path = Path("/nonexistent/quality.db")
    obj.tag_engine = None
    obj.agent_directory = []
    obj.vnx_path = Path("/tmp/vnx")
    obj.project_root = Path("/tmp/project")
    return obj


class TestRunsCleanOnDefaultEnv(unittest.TestCase):
    """Gatherer operates without unhandled exceptions when no DB is available."""

    def test_runs_clean_on_default_env(self) -> None:
        g = _make_gatherer()
        result = g.gather_for_dispatch("test task description", "T1")
        self.assertIsInstance(result, dict)
        self.assertIn("agent_validated", result)
        self.assertFalse(result.get("dispatch_blocked", False))

    def test_all_query_methods_return_safe_defaults_without_db(self) -> None:
        g = _make_gatherer()
        self.assertEqual(g.query_relevant_patterns("some task"), [])
        self.assertEqual(g.find_similar_reports("some task"), [])
        self.assertEqual(g.query_antipatterns("some task"), [])
        self.assertEqual(g._get_session_insights("T1"), [])
        self.assertEqual(g.get_mined_quality_context("some task"), "")


class TestCorruptInputLogsWarning(unittest.TestCase):
    """Corrupt NDJSON input is handled gracefully without raising."""

    def test_corrupt_input_logs_warning(self) -> None:
        """record_adoption_from_receipt skips corrupt lines and returns a valid result."""
        with tempfile.TemporaryDirectory() as tmp:
            g = _make_gatherer()
            log_path = Path(tmp) / "intelligence_usage.ndjson"
            log_path.write_text(
                '{"event_type":"offer","dispatch_id":"d1","pattern_id":"p1","file_path":"f.py"}\n'
                "NOT VALID JSON\n"
                '{"event_type":"offer","dispatch_id":"d1","pattern_id":"p2","file_path":""}\n'
            )
            with patch.object(g, "_usage_log_path", return_value=log_path):
                result = g.record_adoption_from_receipt("d1", "T1", "/nonexistent/report.md")
            self.assertIn("checked", result)
            # 2 parseable offer lines for dispatch d1
            self.assertEqual(result["checked"], 2)


class TestRecordPatternAdoptionDbError(unittest.TestCase):
    """Site 1: record_pattern_adoption — sqlite UPDATE failure logged, not raised."""

    def test_db_error_logged_not_raised(self) -> None:
        g = _make_gatherer()
        mock_db = MagicMock()
        mock_db.execute.side_effect = sqlite3.Error("disk I/O error")
        g.quality_db = mock_db
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            g.record_pattern_adoption("hash123", "T1", "dispatch-001")
        self.assertTrue(any("hash123" in line for line in cm.output))


class TestRegisterOfferedPatternsDbErrors(unittest.TestCase):
    """Sites 2+3: _register_offered_patterns — INSERT and commit errors logged."""

    def test_insert_error_logged_not_raised(self) -> None:
        g = _make_gatherer()
        mock_db = MagicMock()
        mock_db.execute.side_effect = sqlite3.Error("constraint violation")
        mock_db.commit.return_value = None
        g.quality_db = mock_db
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            g._register_offered_patterns(
                [{"pattern_hash": "abc123", "title": "Test"}], dispatch_id="d1"
            )
        self.assertTrue(any("abc123" in line for line in cm.output))

    def test_commit_error_logged_not_raised(self) -> None:
        g = _make_gatherer()
        mock_db = MagicMock()
        mock_db.execute.return_value = None
        mock_db.commit.side_effect = sqlite3.Error("database locked")
        g.quality_db = mock_db
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            g._register_offered_patterns(
                [{"pattern_hash": "def456", "title": "Test 2"}], dispatch_id="d1"
            )
        self.assertTrue(any("commit" in line.lower() for line in cm.output))


class TestVerifyPatternFreshnessDbError(unittest.TestCase):
    """Site 4: _verify_pattern_freshness — sqlite failure logged, not raised."""

    def test_db_error_logged_not_raised(self) -> None:
        g = _make_gatherer()
        mock_db = MagicMock()
        mock_db.execute.side_effect = sqlite3.Error("no such table: snippet_metadata")
        g.quality_db = mock_db
        pattern = {"file_path": "scripts/gather_intelligence.py", "title": "Test"}
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            g._verify_pattern_freshness(pattern)
        self.assertTrue(any("freshness" in line.lower() for line in cm.output))


class TestMainDbErrors(unittest.TestCase):
    """Sites 5+6: main() — pattern count and stats errors logged, main() still exits."""

    def _run_main_with_gatherer(self, g: T0IntelligenceGatherer) -> int:
        with patch("gather_intelligence.T0IntelligenceGatherer", return_value=g):
            with patch("sys.argv", ["gather_intelligence.py"]):
                return gi.main()

    def test_pattern_count_db_error_handled(self) -> None:
        g = _make_gatherer()
        mock_db = MagicMock()
        mock_db.execute.side_effect = sqlite3.Error("no such table: code_snippets")
        g.quality_db = mock_db
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            exit_code = self._run_main_with_gatherer(g)
        self.assertEqual(exit_code, gi.EXIT_OK)
        self.assertTrue(any("pattern count" in line.lower() for line in cm.output))

    def test_tag_engine_stats_error_handled(self) -> None:
        g = _make_gatherer()
        mock_engine = MagicMock()
        mock_engine.get_statistics.side_effect = RuntimeError("stats unavailable")
        g.tag_engine = mock_engine
        with self.assertLogs("gather_intelligence", level="DEBUG") as cm:
            exit_code = self._run_main_with_gatherer(g)
        self.assertEqual(exit_code, gi.EXIT_OK)
        self.assertTrue(any("statistics" in line.lower() for line in cm.output))


if __name__ == "__main__":
    unittest.main()
