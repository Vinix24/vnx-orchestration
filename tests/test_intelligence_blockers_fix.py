#!/usr/bin/env python3
"""
Regression tests for intelligence cluster 3-blocker fix.

Dispatch-ID: 20260517-fix-intelligence-cluster

Tests:
1. MAX_PAYLOAD_CHARS == 2000 (FPC contract compliance)
2. Dead methods removed from IntelligenceSelector
3. _query_candidates passes project_id_fn to all query functions
4. _get_quality_db logs warning on connection failure
5. _get_central_qi_conn logs debug on connection failure
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources._common import MAX_PAYLOAD_CHARS
from intelligence_selector import IntelligenceSelector


class TestMaxPayloadCharsContract(unittest.TestCase):
    """FPC contract mandates MAX_PAYLOAD_CHARS = 2000."""

    def test_max_payload_chars_equals_2000(self):
        self.assertEqual(MAX_PAYLOAD_CHARS, 2000)


class TestDeadMethodsRemoved(unittest.TestCase):
    """Three private query wrappers must not exist on IntelligenceSelector."""

    def test_no_query_proven_patterns_method(self):
        self.assertFalse(hasattr(IntelligenceSelector, "_query_proven_patterns"))

    def test_no_query_failure_prevention_method(self):
        self.assertFalse(hasattr(IntelligenceSelector, "_query_failure_prevention"))

    def test_no_query_recent_comparable_method(self):
        self.assertFalse(hasattr(IntelligenceSelector, "_query_recent_comparable"))


class TestProjectIdFnPassed(unittest.TestCase):
    """_query_candidates must pass project_id_fn=current_project_id to all query functions."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS success_patterns (id INTEGER PRIMARY KEY)")
        conn.close()

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    @patch("intelligence_selector.query_proven_patterns", return_value=[])
    @patch("intelligence_selector.query_failure_prevention", return_value=[])
    @patch("intelligence_selector.query_recent_comparable", return_value=[])
    @patch("intelligence_selector.current_project_id", return_value="test-project")
    def test_project_id_fn_in_kwargs(self, mock_pid, mock_rc, mock_fp, mock_pp):
        selector = IntelligenceSelector(quality_db_path=self.db_path)
        selector._query_candidates("coding_interactive", ["backend-developer"])
        selector.close()

        for call in mock_pp.call_args_list:
            self.assertIn("project_id_fn", call.kwargs)
            self.assertIsNotNone(call.kwargs["project_id_fn"])

        for call in mock_fp.call_args_list:
            self.assertIn("project_id_fn", call.kwargs)
            self.assertIsNotNone(call.kwargs["project_id_fn"])

        for call in mock_rc.call_args_list:
            self.assertIn("project_id_fn", call.kwargs)
            self.assertIsNotNone(call.kwargs["project_id_fn"])


class TestGetQualityDbWarningLog(unittest.TestCase):
    """_get_quality_db must emit logger.warning on connection failure."""

    def test_warning_logged_on_connect_failure(self):
        bad_path = Path("/dev/null/impossible/quality_intelligence.db")
        with patch.object(Path, "exists", return_value=True):
            selector = IntelligenceSelector(quality_db_path=bad_path)
            with self.assertLogs("intelligence_selector", level="WARNING") as cm:
                result = selector.close()
                selector._quality_db_path = bad_path
                selector._quality_db = None
                with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("cannot open")):
                    result = selector._get_quality_db()
            self.assertIsNone(result)
            self.assertTrue(any("_get_quality_db" in msg for msg in cm.output))


class TestGetCentralQiConnDebugLog(unittest.TestCase):
    """_get_central_qi_conn must emit logger.debug on connection failure."""

    @patch("intelligence_selector._resolve_central_data_dir")
    @patch("intelligence_selector.current_project_id", return_value="test-proj")
    def test_debug_logged_on_connect_failure(self, mock_pid, mock_resolve):
        mock_path = MagicMock()
        mock_path.__truediv__ = MagicMock(return_value=mock_path)
        mock_path.exists.return_value = True
        mock_resolve.return_value = mock_path

        selector = IntelligenceSelector()
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("cannot open")):
            with self.assertLogs("intelligence_selector", level="DEBUG") as cm:
                result = selector._get_central_qi_conn()
        self.assertIsNone(result)
        self.assertTrue(any("_get_central_qi_conn" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
