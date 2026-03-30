#!/usr/bin/env python3
"""
Smoke tests for PR-3: Headless run scenarios.

Gate: gate_pr3_operator_inspection
Covers:
  - Success scenario: clean exit, correct classification
  - Timeout scenario: subprocess exceeds timeout, classified as TIMEOUT
  - No-output hang scenario: process silent for > threshold, classified as NO_OUTPUT
  - Interrupted scenario: signal-terminated, classified as INTERRUPTED

These scenarios exercise the full stack: adapter -> registry -> classifier -> artifacts,
validating that operator inspection produces meaningful diagnostics for each case.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts/lib to path
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from runtime_coordination import init_schema, get_connection, register_dispatch, create_attempt
from headless_run_registry import HeadlessRunRegistry, HeadlessRun
from exit_classifier import classify_exit, SUCCESS, TIMEOUT, NO_OUTPUT, INTERRUPTED
from headless_inspect import (
    format_run_line,
    format_run_detail,
    build_health_summary,
    format_health_summary,
    list_runs,
)


class _SmokeTestCase(unittest.TestCase):
    """Base class with temp DB and registry setup."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()
        self.artifact_dir = Path(self._tmpdir) / "artifacts"
        self.artifact_dir.mkdir()

        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")
        self.registry = HeadlessRunRegistry(self.state_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_dispatch_and_attempt(self, dispatch_id: str) -> str:
        """Register a dispatch and create an attempt. Returns attempt_id."""
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id)
            attempt = create_attempt(
                conn,
                dispatch_id=dispatch_id,
                terminal_id="T2",
                attempt_number=1,
            )
            conn.commit()
        return attempt["attempt_id"]

    def _create_run(self, dispatch_id: str, **kwargs) -> HeadlessRun:
        """Create a headless run with defaults."""
        attempt_id = self._create_dispatch_and_attempt(dispatch_id)
        return self.registry.create_run(
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            target_id=kwargs.get("target_id", "headless_claude_cli"),
            target_type=kwargs.get("target_type", "headless_claude_cli"),
            task_class=kwargs.get("task_class", "research_structured"),
            terminal_id=kwargs.get("terminal_id", "T2"),
        )


class TestSmokeSuccess(_SmokeTestCase):
    """Smoke scenario: successful headless run."""

    def test_success_lifecycle(self):
        """A run that succeeds: init -> running -> completing -> succeeded."""
        run = self._create_run("dispatch_success_01")
        self.assertEqual(run.state, "init")

        run = self.registry.transition(
            run.run_id, "running",
            pid=12345, pgid=12345,
            actor="smoke_test",
            reason="subprocess started",
        )
        self.assertEqual(run.state, "running")
        self.assertEqual(run.pid, 12345)
        self.assertIsNotNone(run.heartbeat_at)
        self.assertIsNotNone(run.last_output_at)

        run = self.registry.transition(
            run.run_id, "completing",
            actor="smoke_test",
            reason="exit code 0",
        )
        self.assertEqual(run.state, "completing")

        run = self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0,
            failure_class="SUCCESS",
            duration_seconds=5.2,
            log_artifact_path="/tmp/log.txt",
            output_artifact_path="/tmp/output.txt",
            actor="smoke_test",
            reason="clean exit",
        )
        self.assertEqual(run.state, "succeeded")
        self.assertEqual(run.failure_class, "SUCCESS")
        self.assertEqual(run.exit_code, 0)
        self.assertIsNotNone(run.completed_at)
        self.assertAlmostEqual(run.duration_seconds, 5.2)

    def test_success_classification(self):
        """Exit classifier returns SUCCESS for exit code 0."""
        result = classify_exit(exit_code=0)
        self.assertEqual(result.failure_class, SUCCESS)
        self.assertFalse(result.retryable)
        self.assertEqual(result.operator_hint, "")

    def test_success_inspection(self):
        """Operator can inspect a succeeded run."""
        run = self._create_run("dispatch_success_02")
        self.registry.transition(run.run_id, "running", pid=100, actor="test")
        self.registry.transition(run.run_id, "completing", actor="test")
        self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", duration_seconds=3.0,
            actor="test",
        )

        run = self.registry.get(run.run_id)
        line = format_run_line(run)
        self.assertIn("[+]", line)
        self.assertIn("succeeded", line)

        detail = format_run_detail(run)
        self.assertIn("succeeded", detail)
        self.assertIn("SUCCESS", detail)


class TestSmokeTimeout(_SmokeTestCase):
    """Smoke scenario: headless run times out."""

    def test_timeout_classification(self):
        """Exit classifier returns TIMEOUT when timed_out flag is set."""
        result = classify_exit(exit_code=None, timed_out=True)
        self.assertEqual(result.failure_class, TIMEOUT)
        self.assertTrue(result.retryable)
        self.assertIn("VNX_HEADLESS_TIMEOUT", result.operator_hint)

    def test_timeout_lifecycle(self):
        """A timed-out run transitions: init -> running -> failing -> failed."""
        run = self._create_run("dispatch_timeout_01")
        self.registry.transition(run.run_id, "running", pid=200, actor="test")

        self.registry.transition(
            run.run_id, "failing",
            actor="test",
            reason="subprocess exceeded timeout",
        )
        run = self.registry.transition(
            run.run_id, "failed",
            exit_code=None,
            failure_class="TIMEOUT",
            duration_seconds=600.0,
            actor="test",
            reason="timeout after 600s",
        )
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.failure_class, "TIMEOUT")
        self.assertIsNotNone(run.completed_at)

    def test_timeout_inspection(self):
        """Operator sees TIMEOUT in inspection output."""
        run = self._create_run("dispatch_timeout_02")
        self.registry.transition(run.run_id, "running", pid=201, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            failure_class="TIMEOUT", duration_seconds=600.0, actor="test",
        )

        run = self.registry.get(run.run_id)
        line = format_run_line(run)
        self.assertIn("[X]", line)
        self.assertIn("TIMEOUT", line)

        detail = format_run_detail(run)
        self.assertIn("TIMEOUT", detail)
        self.assertIn("Timed out", detail)


class TestSmokeNoOutputHang(_SmokeTestCase):
    """Smoke scenario: headless run produces no output (hang)."""

    def test_no_output_classification(self):
        """Exit classifier returns NO_OUTPUT when no_output_detected flag is set."""
        result = classify_exit(exit_code=1, no_output_detected=True)
        self.assertEqual(result.failure_class, NO_OUTPUT)
        self.assertTrue(result.retryable)
        self.assertIn("prompt", result.operator_hint.lower())

    def test_no_output_lifecycle(self):
        """A hung run transitions through failing to failed with NO_OUTPUT class."""
        run = self._create_run("dispatch_hang_01")
        self.registry.transition(run.run_id, "running", pid=300, actor="test")

        self.registry.transition(
            run.run_id, "failing",
            actor="test",
            reason="no output for 120s",
        )
        run = self.registry.transition(
            run.run_id, "failed",
            failure_class="NO_OUTPUT",
            duration_seconds=125.0,
            actor="test",
            reason="no-output hang detected",
        )
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.failure_class, "NO_OUTPUT")

    def test_hung_detection(self):
        """Registry detects hung runs via list_hung() query."""
        run = self._create_run("dispatch_hang_02")
        self.registry.transition(run.run_id, "running", pid=301, actor="test")

        # Manually set last_output_at to 200 seconds ago
        with get_connection(self.state_dir) as conn:
            from datetime import datetime, timezone, timedelta
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()

        hung = self.registry.list_hung()
        self.assertEqual(len(hung), 1)
        self.assertEqual(hung[0].run_id, run.run_id)

    def test_no_output_inspection(self):
        """Operator sees NO_OUTPUT in failure details."""
        run = self._create_run("dispatch_hang_03")
        self.registry.transition(run.run_id, "running", pid=302, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            failure_class="NO_OUTPUT", actor="test",
        )

        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)
        self.assertIn("NO_OUTPUT", detail)
        self.assertIn("No output (hang)", detail)


class TestSmokeInterrupted(_SmokeTestCase):
    """Smoke scenario: headless run interrupted by signal."""

    def test_interrupted_classification_sigint(self):
        """Exit classifier returns INTERRUPTED for SIGINT (exit code -2)."""
        result = classify_exit(exit_code=-2)
        self.assertEqual(result.failure_class, INTERRUPTED)
        self.assertTrue(result.retryable)
        self.assertEqual(result.signal, 2)

    def test_interrupted_classification_sigterm(self):
        """Exit classifier returns INTERRUPTED for SIGTERM (exit code -15)."""
        result = classify_exit(exit_code=-15)
        self.assertEqual(result.failure_class, INTERRUPTED)
        self.assertTrue(result.retryable)
        self.assertEqual(result.signal, 15)

    def test_interrupted_lifecycle(self):
        """An interrupted run transitions through failing to failed."""
        run = self._create_run("dispatch_interrupt_01")
        self.registry.transition(run.run_id, "running", pid=400, actor="test")

        self.registry.transition(
            run.run_id, "failing",
            actor="test",
            reason="received SIGINT",
        )
        run = self.registry.transition(
            run.run_id, "failed",
            exit_code=-2,
            failure_class="INTERRUPTED",
            duration_seconds=10.0,
            actor="test",
            reason="killed by signal 2",
        )
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.failure_class, "INTERRUPTED")
        self.assertEqual(run.exit_code, -2)

    def test_interrupted_inspection(self):
        """Operator sees INTERRUPTED with signal details."""
        run = self._create_run("dispatch_interrupt_02")
        self.registry.transition(run.run_id, "running", pid=401, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            exit_code=-15, failure_class="INTERRUPTED", actor="test",
        )

        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)
        self.assertIn("INTERRUPTED", detail)
        self.assertIn("Interrupted (signal)", detail)


class TestSmokeOperatorSummary(_SmokeTestCase):
    """Smoke: operator summary view with mixed run states."""

    def test_summary_with_mixed_states(self):
        """Health summary correctly counts active, succeeded, and failed runs."""
        # Create a succeeded run
        run1 = self._create_run("dispatch_summary_01")
        self.registry.transition(run1.run_id, "running", pid=500, actor="test")
        self.registry.transition(run1.run_id, "completing", actor="test")
        self.registry.transition(
            run1.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", actor="test",
        )

        # Create a failed run
        run2 = self._create_run("dispatch_summary_02")
        self.registry.transition(run2.run_id, "running", pid=501, actor="test")
        self.registry.transition(run2.run_id, "failing", actor="test")
        self.registry.transition(
            run2.run_id, "failed",
            failure_class="TIMEOUT", actor="test",
        )

        # Create an active run
        run3 = self._create_run("dispatch_summary_03")
        self.registry.transition(run3.run_id, "running", pid=502, actor="test")

        summary = build_health_summary(self.registry)
        self.assertEqual(summary.active_count, 1)
        self.assertEqual(summary.succeeded_count, 1)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.status_label, "ACTIVE")
        self.assertEqual(summary.failure_class_counts.get("TIMEOUT"), 1)

        text = format_health_summary(summary)
        self.assertIn("ACTIVE", text)
        self.assertIn("TIMEOUT", text)

    def test_summary_idle(self):
        """Health summary shows IDLE when no runs exist."""
        summary = build_health_summary(self.registry)
        self.assertEqual(summary.status_label, "IDLE")
        self.assertEqual(summary.total_runs, 0)

    def test_list_runs_active_filter(self):
        """list_runs with show_active returns only running runs."""
        run = self._create_run("dispatch_list_01")
        self.registry.transition(run.run_id, "running", pid=600, actor="test")

        lines = list_runs(self.registry, show_active=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("[>]", lines[0])

    def test_list_runs_failed_filter(self):
        """list_runs with show_failed returns only failed runs."""
        run = self._create_run("dispatch_list_02")
        self.registry.transition(run.run_id, "running", pid=601, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            failure_class="TOOL_FAIL", actor="test",
        )

        lines = list_runs(self.registry, show_failed=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("[X]", lines[0])
        self.assertIn("TOOL_FAIL", lines[0])


class TestSmokeRecoveryIntegration(_SmokeTestCase):
    """Smoke: recovery flow detects and reconciles stuck headless runs."""

    def test_stale_run_detected_by_recovery(self):
        """Recovery phase detects stale heartbeat and transitions to failed."""
        run = self._create_run("dispatch_recovery_01")
        self.registry.transition(run.run_id, "running", pid=700, actor="test")

        # Set heartbeat to 120 seconds ago (stale threshold = 60s)
        with get_connection(self.state_dir) as conn:
            from datetime import datetime, timezone, timedelta
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()

        # Verify stale detection
        stale = self.registry.list_stale()
        self.assertEqual(len(stale), 1)

        # Run the recovery phase directly
        from vnx_recover_runtime import _phase_headless_reconciliation, RecoveryReport, _now_utc
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        _phase_headless_reconciliation(self.state_dir, report, dry_run=False)

        # Verify the run was transitioned to failed
        run = self.registry.get(run.run_id)
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.failure_class, "INFRA_FAIL")

        # Verify recovery action was logged
        stale_actions = [a for a in report.actions if a.action == "fail_stale_run"]
        self.assertEqual(len(stale_actions), 1)
        self.assertEqual(stale_actions[0].outcome, "applied")

    def test_hung_run_detected_by_recovery(self):
        """Recovery phase detects no-output hang and transitions to failed."""
        run = self._create_run("dispatch_recovery_02")
        self.registry.transition(run.run_id, "running", pid=701, actor="test")

        # Set last_output_at to 200 seconds ago, but heartbeat is recent
        with get_connection(self.state_dir) as conn:
            from datetime import datetime, timezone, timedelta
            old_output = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
            fresh_hb = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ?, heartbeat_at = ? WHERE run_id = ?",
                (old_output, fresh_hb, run.run_id),
            )
            conn.commit()

        # Verify hung detection
        hung = self.registry.list_hung()
        self.assertEqual(len(hung), 1)

        from vnx_recover_runtime import _phase_headless_reconciliation, RecoveryReport, _now_utc
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        _phase_headless_reconciliation(self.state_dir, report, dry_run=False)

        run = self.registry.get(run.run_id)
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.failure_class, "NO_OUTPUT")

    def test_clean_recovery_no_problems(self):
        """Recovery phase reports clean when no stuck runs exist."""
        from vnx_recover_runtime import _phase_headless_reconciliation, RecoveryReport, _now_utc
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        _phase_headless_reconciliation(self.state_dir, report, dry_run=False)

        clean_actions = [a for a in report.actions if a.action == "check_headless_runs"]
        self.assertEqual(len(clean_actions), 1)


if __name__ == "__main__":
    unittest.main()
