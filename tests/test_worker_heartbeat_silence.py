#!/usr/bin/env python3
"""Tests for worker_heartbeat — event-stream-based worker silence detection.

Covers:
  - EventStreamHeartbeat: silence detection via EventStore
  - FileProgressHeartbeat: silence detection via file growth (tmux-spawn lane)
  - Permission prompt detection in events
  - build_heartbeat_failure_report: terminal failure report generation
  - False-positive guard: progressing worker NOT killed
  - Configurable silence threshold via VNX_WORKER_HEARTBEAT_SILENCE_SECONDS
  - Threshold resolution: negative/zero/unparseable values

Gate: 20260804-124501-worker-heartbeat
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)


def _make_event(ts: str, event_type: str = "tool_use", data: dict | None = None) -> dict:
    return {
        "type": event_type,
        "timestamp": ts,
        "dispatch_id": "test-dispatch-001",
        "terminal": "T1",
        "sequence": 1,
        "data": data or {},
    }


def _now_iso(offset_seconds: float = 0) -> str:
    dt = datetime.now(timezone.utc).timestamp() + offset_seconds
    return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat()


def _write_event_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


class TestEventStreamHeartbeat(unittest.TestCase):
    """Silence detection via EventStore NDJSON stream."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._events_dir = self._tmpdir / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self._tmpdir), ignore_errors=True)

    def test_no_events_not_silent(self):
        from worker_heartbeat import EventStreamHeartbeat
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)
        self.assertEqual(verdict.silence_seconds, 0.0)

    def test_recent_event_not_silent(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-5), "tool_use", {"name": "Edit"})])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)
        self.assertGreater(verdict.silence_seconds, 0)
        self.assertLess(verdict.silence_seconds, 60)

    def test_old_event_is_silent(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-7200), "thinking")])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertTrue(verdict.is_silent)
        self.assertGreater(verdict.silence_seconds, 60)

    def test_threshold_zero_disables(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-7200))])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=0)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)

    def test_event_without_timestamp(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [{"type": "init", "data": {}}])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)

    def test_terminal_isolation(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-10), "tool_use", {"name": "Edit"})])
        _write_event_file(self._events_dir / "T2.ndjson", [_make_event(_now_iso(-7200), "tool_use", {"name": "Read"})])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)


class TestPermissionPromptDetection(unittest.TestCase):
    """Detect permission prompt indicators in event data."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._events_dir = self._tmpdir / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self._tmpdir), ignore_errors=True)

    def test_permission_event_type(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-7200), "ask_permission", {"tool_name": "Bash"})])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertTrue(verdict.is_permission_prompt)

    def test_blocked_reason_field(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-7200), "tool_use_blocked", {"blocked_reason": "permission denied"})])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertTrue(verdict.is_permission_prompt)

    def test_normal_event_not_permission(self):
        from worker_heartbeat import EventStreamHeartbeat
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-5), "tool_use", {"name": "Edit"})])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_permission_prompt)

    def test_raw_indicator_in_data(self):
        from worker_heartbeat import EventStreamHeartbeat
        ev = {"type": "unknown", "timestamp": _now_iso(-7200), "data": {"nested": "approval_required for op"}}
        _write_event_file(self._events_dir / "T1.ndjson", [ev])
        hb = EventStreamHeartbeat("T1", "dispatch-001", events_dir=self._events_dir, silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertTrue(verdict.is_permission_prompt)


class TestFileProgressHeartbeat(unittest.TestCase):
    """Silence detection via file growth (tmux-spawn pipe-pane log)."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self._tmpdir), ignore_errors=True)

    def _make_log(self, content: str = "") -> Path:
        path = self._tmpdir / "test-dispatch.log"
        path.write_text(content)
        return path

    def test_no_file_not_silent(self):
        from worker_heartbeat import FileProgressHeartbeat
        hb = FileProgressHeartbeat(self._tmpdir / "nonexistent.log", "dispatch-001", silence_threshold_seconds=60)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)

    def test_growing_file_not_silent(self):
        from worker_heartbeat import FileProgressHeartbeat
        path = self._make_log("initial\n")
        hb = FileProgressHeartbeat(path, "dispatch-001", silence_threshold_seconds=60)
        hb.update()
        with open(path, "a") as f:
            f.write("more\n")
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)

    def test_static_file_silent(self):
        from worker_heartbeat import FileProgressHeartbeat
        path = self._make_log("content\n")
        hb = FileProgressHeartbeat(path, "dispatch-001", silence_threshold_seconds=0.1)
        hb.update()
        time.sleep(0.2)
        verdict = hb.check()
        self.assertTrue(verdict.is_silent)

    def test_threshold_zero_disables(self):
        from worker_heartbeat import FileProgressHeartbeat
        path = self._make_log("content\n")
        hb = FileProgressHeartbeat(path, "dispatch-001", silence_threshold_seconds=0)
        hb.update()
        time.sleep(0.2)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)


class TestBuildFailureReport(unittest.TestCase):
    """build_heartbeat_failure_report generates valid unified report text."""

    def test_report_has_required_sections(self):
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report
        verdict = SilenceVerdict(is_silent=True, silence_seconds=650.0, threshold_seconds=600,
                                 last_event={"type": "tool_use", "timestamp": _now_iso(-650), "data": {"name": "Edit"}},
                                 last_event_timestamp=_now_iso(-650))
        report = build_heartbeat_failure_report("dispatch-001", verdict, terminal_id="T1")
        self.assertIn("## Summary", report)
        self.assertIn("## Changes", report)
        self.assertIn("## Verification", report)
        self.assertIn("## Open Items", report)
        self.assertIn("dispatch-001", report)

    def test_report_permission_reason(self):
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report
        verdict = SilenceVerdict(is_silent=True, silence_seconds=900.0, threshold_seconds=600,
                                 last_event={"type": "ask_permission", "timestamp": _now_iso(-900)},
                                 last_event_timestamp=_now_iso(-900),
                                 is_permission_prompt=True, permission_reason="event type contains 'permission'")
        report = build_heartbeat_failure_report("dispatch-002", verdict, terminal_id="T2")
        self.assertIn("permission prompt", report)

    def test_summary_meets_minimum_length(self):
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report
        verdict = SilenceVerdict(is_silent=True, silence_seconds=600, threshold_seconds=600)
        report = build_heartbeat_failure_report("dispatch-003", verdict, terminal_id="T3")
        summary_start = report.find("## Summary")
        changes_start = report.find("## Changes")
        summary_body = report[summary_start:changes_start]
        summary_text = "".join(summary_body.split())
        self.assertGreaterEqual(len(summary_text), 50)

    def test_report_carries_structural_failure_status(self):
        # OI-1130: the failure must be a parseable STATUS FIELD, not Summary
        # prose.  Assert on the extracted fields, never on prose text.
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report
        from report_to_receipt_converter import _extract_body_fields
        verdict = SilenceVerdict(
            is_silent=True, silence_seconds=1900.0, threshold_seconds=1800,
        )
        report = build_heartbeat_failure_report(
            "20260811-hb-kill", verdict, terminal_id="T1",
        )
        fields = _extract_body_fields(report)
        self.assertEqual(fields.get("status"), "failed")
        self.assertEqual(fields.get("failure_reason"), "heartbeat_killed")

    def test_kill_report_converts_to_task_failed_receipt(self):
        # OI-1130 end-to-end shape: the heartbeat-kill report must land as a
        # task_failed receipt, never task_complete.  Before the fix the report
        # passed the body contract with no status field at all -> the
        # converter emitted event_type="task_complete", status="" — a kill
        # indistinguishable from success without reading the Summary.
        from worker_heartbeat import SilenceVerdict, build_heartbeat_failure_report
        from report_to_receipt_converter import build_receipt_from_report
        verdict = SilenceVerdict(
            is_silent=True, silence_seconds=1900.0, threshold_seconds=1800,
        )
        report = build_heartbeat_failure_report(
            "20260811-hb-kill-receipt", verdict, terminal_id="T1",
        )
        tmpdir = Path(tempfile.mkdtemp())
        path = tmpdir / "20260811-hb-kill-receipt.md"
        path.write_text(report, encoding="utf-8")
        try:
            receipt = build_receipt_from_report(path, report)
        finally:
            import shutil
            shutil.rmtree(str(tmpdir), ignore_errors=True)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["event_type"], "task_failed")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt.get("failure_reason"), "heartbeat_killed")


class TestThresholdResolution(unittest.TestCase):
    """VNX_WORKER_HEARTBEAT_SILENCE_SECONDS env var resolution."""

    def test_default_unset(self):
        # OI-1130: default raised 600 -> 1800.  The 600s tail was measured on
        # the chatty subprocess event stream; deep-thinking tmux workers are
        # legitimately silent past 600s (4 of 5 killed on 2026-08-10 with
        # real work in their worktrees).
        with patch.dict(os.environ, {}, clear=True):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 1800)

    def test_valid_env_value(self):
        with patch.dict(os.environ, {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "300"}):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 300)

    def test_env_override_above_default_wins(self):
        # The override must win in BOTH directions — 3600 > default 1800.
        with patch.dict(os.environ, {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "3600"}):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 3600)

    def test_negative_clamps(self):
        with patch.dict(os.environ, {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "-100"}):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 1800)

    def test_unparseable_clamps(self):
        with patch.dict(os.environ, {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "abc"}):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 1800)

    def test_zero_disables(self):
        with patch.dict(os.environ, {"VNX_WORKER_HEARTBEAT_SILENCE_SECONDS": "0"}):
            from worker_heartbeat import _resolve_silence_threshold
            self.assertEqual(_resolve_silence_threshold(), 0)


class TestFalsePositiveGuard(unittest.TestCase):
    """A slowly progressing worker must NOT be killed."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._events_dir = self._tmpdir / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self._tmpdir), ignore_errors=True)

    def test_slow_worker_within_threshold_not_silent(self):
        from worker_heartbeat import EventStreamHeartbeat
        threshold = 600
        gap = 290
        _write_event_file(self._events_dir / "T1.ndjson", [_make_event(_now_iso(-gap), "tool_use", {"name": "RunTests"})])
        hb = EventStreamHeartbeat("T1", "dispatch-slow", events_dir=self._events_dir, silence_threshold_seconds=threshold)
        verdict = hb.check()
        self.assertFalse(verdict.is_silent,
                        f"False positive: slow worker ({gap}s gap) flagged as silent with threshold={threshold}s")

    def test_file_heartbeat_slow_growth_not_silent(self):
        from worker_heartbeat import FileProgressHeartbeat
        path = self._tmpdir / "slow.log"
        path.write_text("line 1\n")
        hb = FileProgressHeartbeat(path, "dispatch-slow", silence_threshold_seconds=0.3)
        hb.update()
        time.sleep(0.1)
        with open(path, "a") as f:
            f.write("line 2\n")
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)
        time.sleep(0.15)
        with open(path, "a") as f:
            f.write("line 3\n")
        verdict = hb.check()
        self.assertFalse(verdict.is_silent)
        time.sleep(0.4)
        verdict = hb.check()
        self.assertTrue(verdict.is_silent)


class TestSilenceVerdict(unittest.TestCase):
    """SilenceVerdict dataclass fields."""

    def test_defaults(self):
        from worker_heartbeat import SilenceVerdict
        v = SilenceVerdict(is_silent=False, silence_seconds=0.0, threshold_seconds=600)
        self.assertFalse(v.is_silent)
        self.assertIsNone(v.last_event)
        self.assertFalse(v.is_permission_prompt)
        self.assertEqual(v.permission_reason, "")

    def test_permission_fields(self):
        from worker_heartbeat import SilenceVerdict
        ev = {"type": "ask_permission", "timestamp": "2026-08-04T10:00:00Z"}
        v = SilenceVerdict(is_silent=True, silence_seconds=900.0, threshold_seconds=600,
                          last_event=ev, last_event_timestamp="2026-08-04T10:00:00Z",
                          is_permission_prompt=True, permission_reason="test")
        self.assertTrue(v.is_silent)
        self.assertEqual(v.last_event, ev)
        self.assertTrue(v.is_permission_prompt)
