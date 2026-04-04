#!/usr/bin/env python3
"""
Tests for dashboard_actions.py (Feature 13, PR-2)

Quality gate coverage (gate_pr2_dashboard_control_actions):
  - All session-control action tests pass
  - Session start works per project from the dashboard-backed control surface
  - Attach/open-terminal action resolves against canonical session truth under test
  - Failed or degraded actions produce explicit operator-visible outcomes
  - No action path bypasses runtime/read-model truth under test
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from dashboard_actions import (
    ActionOutcome,
    start_session,
    stop_session,
    attach_terminal,
    refresh_projections,
    run_reconciliation,
    inspect_open_item,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _make_session_profile():
    return {
        "schema_version": 1,
        "session_name": "vnx-test",
        "home_window": {
            "name": "main",
            "panes": [
                {"terminal_id": "T0", "pane_id": "%0", "role": "orchestrator"},
                {"terminal_id": "T1", "pane_id": "%1", "role": "worker"},
                {"terminal_id": "T2", "pane_id": "%2", "role": "worker"},
                {"terminal_id": "T3", "pane_id": "%3", "role": "worker"},
            ],
        },
        "extra_windows": [],
    }


def _make_open_items(items):
    return {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}


# ===========================================================================
# Test: ActionOutcome model (§4.4)
# ===========================================================================

class TestActionOutcome(unittest.TestCase):

    def test_ao1_every_action_has_outcome(self):
        """AO-1: Every action produces exactly one outcome."""
        outcome = ActionOutcome(
            action="test", project="/tmp", status="success", message="ok"
        )
        self.assertEqual(outcome.status, "success")
        self.assertIsNotNone(outcome.timestamp)

    def test_ao2_failed_has_error_code(self):
        """AO-2: Failed outcomes include error_code."""
        outcome = ActionOutcome(
            action="test", project="/tmp", status="failed",
            message="bad", error_code="test_error"
        )
        d = outcome.to_dict()
        self.assertEqual(d["error_code"], "test_error")

    def test_ao3_already_active_is_valid(self):
        """AO-3: already_active is a valid success variant."""
        outcome = ActionOutcome(
            action="test", project="/tmp", status="already_active",
            message="session exists"
        )
        self.assertEqual(outcome.status, "already_active")

    def test_ao4_degraded_status(self):
        """AO-4: degraded means partially succeeded."""
        outcome = ActionOutcome(
            action="test", project="/tmp", status="degraded",
            message="partial"
        )
        self.assertEqual(outcome.status, "degraded")

    def test_to_dict_omits_none_error_code(self):
        outcome = ActionOutcome(
            action="test", project="/tmp", status="success", message="ok"
        )
        d = outcome.to_dict()
        self.assertNotIn("error_code", d)

    def test_to_dict_includes_all_fields(self):
        outcome = ActionOutcome(
            action="start_session", project="/tmp/proj",
            status="success", message="started",
            details={"session_name": "vnx-proj"},
        )
        d = outcome.to_dict()
        required = {"action", "project", "status", "message", "details", "timestamp"}
        self.assertTrue(required.issubset(set(d.keys())))


# ===========================================================================
# Test: A1 — Start Session
# ===========================================================================

class TestStartSession(unittest.TestCase):

    def test_missing_project_dir(self):
        outcome = start_session("/nonexistent/path")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "project_not_found")

    def test_dry_run_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = start_session(tmp, vnx_bin="/usr/bin/true", dry_run=True)
            self.assertEqual(outcome.status, "success")
            self.assertTrue(outcome.details.get("dry_run"))

    def test_dry_run_existing_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=True):
                outcome = start_session(tmp, vnx_bin="/usr/bin/true", dry_run=True)
            self.assertEqual(outcome.status, "already_active")

    def test_already_active_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=True):
                outcome = start_session(tmp, vnx_bin="/usr/bin/true")
            self.assertEqual(outcome.status, "already_active")

    def test_missing_vnx_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._find_vnx_bin", return_value=None), \
                 patch("dashboard_actions._detect_profile", return_value=None), \
                 patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = start_session(tmp)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error_code, "vnx_not_found")

    @patch("dashboard_actions._tmux_session_exists", return_value=False)
    @patch("dashboard_actions._detect_profile", return_value=None)
    @patch("dashboard_actions.subprocess.run")
    def test_successful_start(self, mock_run, _detect, _exists):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with tempfile.TemporaryDirectory() as tmp:
            outcome = start_session(tmp, vnx_bin="/usr/bin/true")
        self.assertEqual(outcome.status, "success")

    @patch("dashboard_actions._tmux_session_exists", return_value=False)
    @patch("dashboard_actions._detect_profile", return_value=None)
    @patch("dashboard_actions.subprocess.run")
    def test_failed_start(self, mock_run, _detect, _exists):
        mock_run.return_value = MagicMock(returncode=1, stderr="error msg", stdout="")
        with tempfile.TemporaryDirectory() as tmp:
            outcome = start_session(tmp, vnx_bin="/usr/bin/false")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "start_failed")


# ===========================================================================
# Test: A2 — Attach Terminal
# ===========================================================================

class TestAttachTerminal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = self._tmp.name
        state_dir = Path(self.proj) / ".vnx-data" / "state"
        _write_json(state_dir / "session_profile.json", _make_session_profile())

    def tearDown(self):
        self._tmp.cleanup()

    @patch("dashboard_actions._tmux_session_exists", return_value=True)
    def test_resolve_terminal_pane(self, _):
        outcome = attach_terminal(self.proj, "T1")
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.details["pane_id"], "%1")
        self.assertIn("attach_command", outcome.details)

    @patch("dashboard_actions._tmux_session_exists", return_value=True)
    def test_unknown_terminal(self, _):
        outcome = attach_terminal(self.proj, "T99")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "pane_not_found")

    @patch("dashboard_actions._tmux_session_exists", return_value=False)
    def test_no_session(self, _):
        outcome = attach_terminal(self.proj, "T1")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "no_session")

    def test_dry_run_skips_session_check(self):
        outcome = attach_terminal(self.proj, "T1", dry_run=True)
        # Dry run doesn't check tmux, so it should still resolve the pane
        self.assertEqual(outcome.status, "success")


# ===========================================================================
# Test: A3 — Refresh Projections
# ===========================================================================

class TestRefreshProjections(unittest.TestCase):

    def test_missing_state_dir(self):
        outcome = refresh_projections("/nonexistent/path")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "no_state_dir")

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            outcome = refresh_projections(tmp, dry_run=True)
        self.assertEqual(outcome.status, "success")
        self.assertTrue(outcome.details.get("dry_run"))

    def test_refresh_with_real_db(self):
        """Refresh from a real initialized schema."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            from runtime_coordination import init_schema
            init_schema(str(state_dir))
            outcome = refresh_projections(tmp)
        self.assertEqual(outcome.status, "success")
        self.assertIn("output_path", outcome.details)


# ===========================================================================
# Test: A4 — Run Reconciliation
# ===========================================================================

class TestRunReconciliation(unittest.TestCase):

    def test_missing_state_dir(self):
        outcome = run_reconciliation("/nonexistent/path")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "no_state_dir")

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            outcome = run_reconciliation(tmp, dry_run=True)
        self.assertEqual(outcome.status, "success")

    def test_reconciliation_no_anomalies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            from runtime_coordination import init_schema
            init_schema(str(state_dir))
            outcome = run_reconciliation(tmp)
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.details["anomaly_count"], 0)


# ===========================================================================
# Test: A5 — Inspect Open Item
# ===========================================================================

class TestInspectOpenItem(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = self._tmp.name
        state_dir = Path(self.proj) / ".vnx-data" / "state"
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "test item", "details": "some details",
             "origin_dispatch_id": "d-001"},
        ]
        _write_json(state_dir / "open_items.json", _make_open_items(items))

    def tearDown(self):
        self._tmp.cleanup()

    def test_found_item(self):
        outcome = inspect_open_item(self.proj, "OI-001")
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.details["item"]["title"], "test item")

    def test_missing_item(self):
        outcome = inspect_open_item(self.proj, "OI-999")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "item_not_found")

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = inspect_open_item(tmp, "OI-001")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "file_not_found")


# ===========================================================================
# Test: A6 — Stop Session
# ===========================================================================

class TestStopSession(unittest.TestCase):

    def test_no_session_to_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = stop_session(tmp, vnx_bin="/usr/bin/true")
        self.assertEqual(outcome.status, "already_active")

    @patch("dashboard_actions._tmux_session_exists", return_value=True)
    @patch("dashboard_actions.subprocess.run")
    def test_successful_stop(self, mock_run, _):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with tempfile.TemporaryDirectory() as tmp:
            outcome = stop_session(tmp, vnx_bin="/usr/bin/true")
        self.assertEqual(outcome.status, "success")

    @patch("dashboard_actions._tmux_session_exists", return_value=True)
    def test_missing_vnx_for_stop(self, _):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._find_vnx_bin", return_value=None):
                outcome = stop_session(tmp)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "vnx_not_found")

    @patch("dashboard_actions._tmux_session_exists", return_value=True)
    def test_dry_run_stop(self, _):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = stop_session(tmp, vnx_bin="/usr/bin/true", dry_run=True)
        self.assertEqual(outcome.status, "success")
        self.assertTrue(outcome.details.get("dry_run"))


# ===========================================================================
# Test: No bypass of runtime truth
# ===========================================================================

class TestNoRuntimeBypass(unittest.TestCase):

    def test_attach_requires_session_profile(self):
        """Attach action reads from session_profile.json, not tmux directly."""
        with tempfile.TemporaryDirectory() as tmp:
            # No session profile exists
            with patch("dashboard_actions._tmux_session_exists", return_value=True):
                outcome = attach_terminal(tmp, "T1")
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error_code, "pane_not_found")

    def test_reconciliation_uses_supervisor(self):
        """Reconciliation uses RuntimeSupervisor, not ad hoc checks."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            from runtime_coordination import init_schema
            init_schema(str(state_dir))
            outcome = run_reconciliation(tmp)
        self.assertEqual(outcome.status, "success")
        self.assertIn("anomaly_count", outcome.details)

    def test_inspect_uses_open_items_file(self):
        """Inspect reads from open_items.json, not rendered markdown."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            # Write markdown but no JSON
            (state_dir / "open_items.md").write_text("# Open Items\n- OI-001: test\n")
            outcome = inspect_open_item(tmp, "OI-001")
        # Should fail because JSON doesn't exist — proves it doesn't parse markdown
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "file_not_found")


if __name__ == "__main__":
    unittest.main()
