#!/usr/bin/env python3
"""
Tests for PR-3: Headless Inspection Module.

Gate: gate_pr3_operator_inspection
Covers:
  - format_run_line produces correct icons and fields
  - format_run_detail includes all operator-relevant information
  - list_runs filters correctly by state, active, failed, problems
  - build_health_summary aggregates counts accurately
  - format_health_summary renders operator-readable output
  - _resolve_run supports prefix matching
  - Liveness warnings appear for stale/hung running runs
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add scripts/lib to path
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from runtime_coordination import init_schema, get_connection, register_dispatch, create_attempt
from headless_run_registry import HeadlessRunRegistry, HeadlessRun
from headless_inspect import (
    format_run_line,
    format_run_detail,
    list_runs,
    build_health_summary,
    format_health_summary,
    HealthSummary,
    _elapsed,
    _ago,
    _short_id,
    _ts_display,
    _resolve_run,
    _run_to_dict,
    STATE_ICONS,
    FAILURE_CLASS_LABELS,
)


class _InspectTestCase(unittest.TestCase):
    """Base class with temp DB and registry setup."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()

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
        attempt_id = self._create_dispatch_and_attempt(dispatch_id)
        return self.registry.create_run(
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            target_id=kwargs.get("target_id", "headless_claude_cli"),
            target_type=kwargs.get("target_type", "headless_claude_cli"),
            task_class=kwargs.get("task_class", "research_structured"),
            terminal_id=kwargs.get("terminal_id", "T2"),
        )


class TestFormatHelpers(unittest.TestCase):
    """Test formatting helper functions."""

    def test_short_id(self):
        self.assertEqual(_short_id("abcdef12-3456-7890"), "abcdef12")
        self.assertEqual(_short_id(None), "-")
        self.assertEqual(_short_id(""), "-")

    def test_ts_display(self):
        self.assertEqual(_ts_display(None), "-")
        result = _ts_display("2026-03-30T10:15:30.000000Z")
        self.assertIn("10:15:30", result)

    def test_elapsed_none(self):
        self.assertEqual(_elapsed(None), "-")

    def test_ago_none(self):
        self.assertEqual(_ago(None), "never")


class TestFormatRunLine(_InspectTestCase):
    """Test format_run_line output."""

    def test_init_state_icon(self):
        run = self._create_run("dispatch_line_01")
        line = format_run_line(run)
        self.assertIn("[.]", line)
        self.assertIn("init", line)

    def test_running_state_icon(self):
        run = self._create_run("dispatch_line_02")
        self.registry.transition(run.run_id, "running", pid=100, actor="test")
        run = self.registry.get(run.run_id)
        line = format_run_line(run)
        self.assertIn("[>]", line)
        self.assertIn("running", line)

    def test_succeeded_state_icon(self):
        run = self._create_run("dispatch_line_03")
        self.registry.transition(run.run_id, "running", pid=100, actor="test")
        self.registry.transition(run.run_id, "completing", actor="test")
        self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", duration_seconds=2.5, actor="test",
        )
        run = self.registry.get(run.run_id)
        line = format_run_line(run)
        self.assertIn("[+]", line)
        self.assertIn("succeeded", line)
        self.assertIn("2.5s", line)

    def test_failed_state_shows_exit_class(self):
        run = self._create_run("dispatch_line_04")
        self.registry.transition(run.run_id, "running", pid=100, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            failure_class="TOOL_FAIL", actor="test",
        )
        run = self.registry.get(run.run_id)
        line = format_run_line(run)
        self.assertIn("[X]", line)
        self.assertIn("TOOL_FAIL", line)

    def test_dispatch_id_short(self):
        run = self._create_run("dispatch_line_05")
        line = format_run_line(run)
        self.assertIn("dispatch=dispatch", line)


class TestFormatRunDetail(_InspectTestCase):
    """Test format_run_detail output."""

    def test_detail_includes_all_fields(self):
        run = self._create_run("dispatch_detail_01")
        self.registry.transition(run.run_id, "running", pid=200, pgid=200, actor="test")
        self.registry.transition(run.run_id, "completing", actor="test")
        self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", duration_seconds=4.0,
            log_artifact_path="/tmp/log.txt",
            output_artifact_path="/tmp/output.txt",
            actor="test",
        )
        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)

        self.assertIn(run.run_id, detail)
        self.assertIn(run.dispatch_id, detail)
        self.assertIn("succeeded", detail)
        self.assertIn("200", detail)  # PID
        self.assertIn("research_structured", detail)
        self.assertIn("SUCCESS", detail)
        self.assertIn("/tmp/log.txt", detail)
        self.assertIn("/tmp/output.txt", detail)
        self.assertIn("4.0s", detail)

    def test_detail_shows_warnings_for_stale_running(self):
        """Running run with stale heartbeat shows warning in detail view."""
        run = self._create_run("dispatch_detail_02")
        self.registry.transition(run.run_id, "running", pid=201, actor="test")

        # Make heartbeat stale (>60s)
        with get_connection(self.state_dir) as conn:
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()

        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)
        self.assertIn("STALE HEARTBEAT", detail)

    def test_detail_shows_warnings_for_hung_running(self):
        """Running run with no recent output shows warning in detail view."""
        run = self._create_run("dispatch_detail_03")
        self.registry.transition(run.run_id, "running", pid=202, actor="test")

        # Make last_output stale (>120s)
        with get_connection(self.state_dir) as conn:
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()

        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)
        self.assertIn("NO OUTPUT", detail)

    def test_detail_no_warnings_for_terminal_state(self):
        """Succeeded run should not show liveness warnings."""
        run = self._create_run("dispatch_detail_04")
        self.registry.transition(run.run_id, "running", pid=203, actor="test")
        self.registry.transition(run.run_id, "completing", actor="test")
        self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", actor="test",
        )
        run = self.registry.get(run.run_id)
        detail = format_run_detail(run)
        self.assertNotIn("STALE HEARTBEAT", detail)
        self.assertNotIn("NO OUTPUT", detail)
        self.assertNotIn("Warnings", detail)


class TestListRuns(_InspectTestCase):
    """Test list_runs filtering."""

    def _populate_mixed(self):
        """Create runs in various states."""
        # Active run
        r1 = self._create_run("dispatch_lm_01")
        self.registry.transition(r1.run_id, "running", pid=300, actor="test")

        # Succeeded run
        r2 = self._create_run("dispatch_lm_02")
        self.registry.transition(r2.run_id, "running", pid=301, actor="test")
        self.registry.transition(r2.run_id, "completing", actor="test")
        self.registry.transition(
            r2.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", actor="test",
        )

        # Failed run
        r3 = self._create_run("dispatch_lm_03")
        self.registry.transition(r3.run_id, "running", pid=302, actor="test")
        self.registry.transition(r3.run_id, "failing", actor="test")
        self.registry.transition(
            r3.run_id, "failed",
            failure_class="TIMEOUT", actor="test",
        )
        return r1, r2, r3

    def test_list_all(self):
        self._populate_mixed()
        lines = list_runs(self.registry)
        self.assertEqual(len(lines), 3)

    def test_list_active_only(self):
        self._populate_mixed()
        lines = list_runs(self.registry, show_active=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("[>]", lines[0])

    def test_list_failed_only(self):
        self._populate_mixed()
        lines = list_runs(self.registry, show_failed=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("[X]", lines[0])

    def test_list_by_state(self):
        self._populate_mixed()
        lines = list_runs(self.registry, filter_state="succeeded")
        self.assertEqual(len(lines), 1)
        self.assertIn("[+]", lines[0])

    def test_list_empty(self):
        lines = list_runs(self.registry)
        self.assertEqual(len(lines), 0)

    def test_list_limit(self):
        self._populate_mixed()
        lines = list_runs(self.registry, limit=2)
        self.assertEqual(len(lines), 2)


class TestHealthSummary(_InspectTestCase):
    """Test build_health_summary and format_health_summary."""

    def test_idle_summary(self):
        summary = build_health_summary(self.registry)
        self.assertEqual(summary.status_label, "IDLE")
        self.assertEqual(summary.total_runs, 0)
        self.assertFalse(summary.has_problems)

    def test_active_summary(self):
        run = self._create_run("dispatch_hs_01")
        self.registry.transition(run.run_id, "running", pid=400, actor="test")

        summary = build_health_summary(self.registry)
        self.assertEqual(summary.status_label, "ACTIVE")
        self.assertEqual(summary.active_count, 1)

    def test_degraded_summary(self):
        run = self._create_run("dispatch_hs_02")
        self.registry.transition(run.run_id, "running", pid=401, actor="test")
        self.registry.transition(run.run_id, "failing", actor="test")
        self.registry.transition(
            run.run_id, "failed",
            failure_class="INFRA_FAIL", actor="test",
        )

        summary = build_health_summary(self.registry)
        self.assertEqual(summary.status_label, "DEGRADED")
        self.assertEqual(summary.failed_count, 1)

    def test_attention_summary_with_problems(self):
        run = self._create_run("dispatch_hs_03")
        self.registry.transition(run.run_id, "running", pid=402, actor="test")

        # Make stale
        with get_connection(self.state_dir) as conn:
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()

        summary = build_health_summary(self.registry)
        self.assertEqual(summary.status_label, "ATTENTION")
        self.assertTrue(summary.has_problems)
        self.assertEqual(summary.stale_count, 1)

    def test_failure_class_counts(self):
        for i, fc in enumerate(["TIMEOUT", "TIMEOUT", "TOOL_FAIL"]):
            run = self._create_run(f"dispatch_fc_{i}")
            self.registry.transition(run.run_id, "running", pid=500 + i, actor="test")
            self.registry.transition(run.run_id, "failing", actor="test")
            self.registry.transition(
                run.run_id, "failed",
                failure_class=fc, actor="test",
            )

        summary = build_health_summary(self.registry)
        self.assertEqual(summary.failure_class_counts["TIMEOUT"], 2)
        self.assertEqual(summary.failure_class_counts["TOOL_FAIL"], 1)

    def test_format_summary_output(self):
        run = self._create_run("dispatch_fmt_01")
        self.registry.transition(run.run_id, "running", pid=600, actor="test")
        self.registry.transition(run.run_id, "completing", actor="test")
        self.registry.transition(
            run.run_id, "succeeded",
            exit_code=0, failure_class="SUCCESS", actor="test",
        )

        summary = build_health_summary(self.registry)
        text = format_health_summary(summary)
        self.assertIn("VNX Headless Health Summary", text)
        self.assertIn("Succeeded:  1", text)


class TestResolveRun(_InspectTestCase):
    """Test _resolve_run prefix matching."""

    def test_full_id_match(self):
        run = self._create_run("dispatch_resolve_01")
        found = _resolve_run(self.registry, run.run_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, run.run_id)

    def test_prefix_match(self):
        run = self._create_run("dispatch_resolve_02")
        prefix = run.run_id[:8]
        found = _resolve_run(self.registry, prefix)
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, run.run_id)

    def test_no_match(self):
        found = _resolve_run(self.registry, "nonexistent-id")
        self.assertIsNone(found)


class TestRunToDict(_InspectTestCase):
    """Test _run_to_dict serialization."""

    def test_dict_has_all_keys(self):
        run = self._create_run("dispatch_dict_01")
        d = _run_to_dict(run)
        self.assertIn("run_id", d)
        self.assertIn("dispatch_id", d)
        self.assertIn("state", d)
        self.assertIn("failure_class", d)
        self.assertIn("pid", d)
        self.assertIn("heartbeat_at", d)
        # Should be JSON-serializable
        json.dumps(d)


if __name__ == "__main__":
    unittest.main()
