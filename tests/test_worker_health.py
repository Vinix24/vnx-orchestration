#!/usr/bin/env python3
"""Tests for F52-PR3: WorkerHealthMonitor.

Gate: f52-pr3
Covers:
  - test_active_status: recent events yield ACTIVE status
  - test_stuck_detection: no events for 120s+ yields STUCK status
  - test_health_json_written: health snapshot persisted to worker_health.json
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from worker_health_monitor import (
    WorkerHealthMonitor,
    WorkerHealth,
    HealthStatus,
    ACTIVE_THRESHOLD,
    SLOW_THRESHOLD,
)


def _make_tool_event(tool_name: str) -> dict:
    return {"type": "tool_use", "data": {"name": tool_name, "input": {}, "id": "x"}}


def _make_text_event() -> dict:
    return {"type": "text", "data": {"text": "hello"}}


class TestActiveStatus(unittest.TestCase):
    """Monitor reports ACTIVE when events were received recently."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_active_status_after_event(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-001", events_dir=self._events_dir
        )
        monitor.stop()  # stop writer thread, we control manually

        monitor.update(_make_tool_event("Edit"))
        h = monitor.health_status()

        self.assertEqual(h.status, HealthStatus.ACTIVE)
        self.assertEqual(h.event_count, 1)
        self.assertEqual(h.last_tool, "Edit")
        self.assertEqual(h.dispatch_id, "dispatch-001")
        self.assertEqual(h.terminal_id, "T1")

    def test_active_status_with_stream_event_object(self):
        """Accepts StreamEvent-like objects with .type and .data attributes."""
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-002", events_dir=self._events_dir
        )
        monitor.stop()

        event = MagicMock()
        event.type = "tool_use"
        event.data = {"name": "Read", "input": {}, "id": "y"}

        monitor.update(event)
        h = monitor.health_status()

        self.assertEqual(h.status, HealthStatus.ACTIVE)
        self.assertEqual(h.last_tool, "Read")

    def test_multiple_events_increment_count(self):
        monitor = WorkerHealthMonitor(
            "T2", "dispatch-003", events_dir=self._events_dir
        )
        monitor.stop()

        for _ in range(5):
            monitor.update(_make_text_event())
        monitor.update(_make_tool_event("Bash"))

        h = monitor.health_status()
        self.assertEqual(h.event_count, 6)
        self.assertEqual(h.last_tool, "Bash")

    def test_completed_status_after_mark_completed(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-004", events_dir=self._events_dir
        )
        monitor.update(_make_text_event())
        monitor.mark_completed()

        h = monitor.health_status()
        self.assertEqual(h.status, HealthStatus.COMPLETED)

    def test_estimated_progress(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-005", events_dir=self._events_dir, avg_events=100
        )
        monitor.stop()

        for _ in range(50):
            monitor.update(_make_text_event())

        progress = monitor.estimated_progress()
        self.assertAlmostEqual(progress, 0.5)

    def test_estimated_progress_capped_at_1(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-006", events_dir=self._events_dir, avg_events=10
        )
        monitor.stop()

        for _ in range(20):
            monitor.update(_make_text_event())

        progress = monitor.estimated_progress()
        self.assertEqual(progress, 1.0)


class TestStuckDetection(unittest.TestCase):
    """Monitor reports STUCK when no events received for 120s+."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_stuck_detection_via_mocked_time(self):
        """Inject a last_event_time far in the past to trigger STUCK."""
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-stuck-001", events_dir=self._events_dir
        )
        monitor.stop()

        # Manually set last event time to simulate 130s of silence
        with monitor._lock:
            monitor._last_event_time = time.monotonic() - (SLOW_THRESHOLD + 10)

        h = monitor.health_status()
        self.assertEqual(h.status, HealthStatus.STUCK)

    def test_slow_detection_between_60_and_120s(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-slow-001", events_dir=self._events_dir
        )
        monitor.stop()

        with monitor._lock:
            # 90s of silence → SLOW (60 < 90 < 120)
            monitor._last_event_time = time.monotonic() - (ACTIVE_THRESHOLD + 30)

        h = monitor.health_status()
        self.assertEqual(h.status, HealthStatus.SLOW)

    def test_stuck_log_event_written(self):
        """log_stuck_event writes an NDJSON entry when status is STUCK."""
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-stuck-002", events_dir=self._events_dir
        )
        monitor.stop()

        with monitor._lock:
            monitor._last_event_time = time.monotonic() - (SLOW_THRESHOLD + 10)

        stuck_log = self._events_dir / "worker_stuck.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertTrue(stuck_log.exists())
        lines = [l for l in stuck_log.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["terminal_id"], "T1")
        self.assertEqual(entry["dispatch_id"], "dispatch-stuck-002")

    def test_stuck_log_not_written_when_active(self):
        """log_stuck_event does nothing when status is not STUCK."""
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-active-003", events_dir=self._events_dir
        )
        monitor.stop()
        monitor.update(_make_text_event())  # recent event → ACTIVE

        stuck_log = self._events_dir / "worker_stuck_active.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertFalse(stuck_log.exists())


class TestHealthJsonWritten(unittest.TestCase):
    """Health snapshot is persisted to worker_health.json."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_health_json_written_on_completion(self):
        """mark_completed() triggers a final health snapshot write."""
        monitor = WorkerHealthMonitor(
            "T2", "dispatch-health-001", events_dir=self._events_dir
        )
        for _ in range(10):
            monitor.update(_make_tool_event("Read"))
        monitor.mark_completed()

        health_path = self._events_dir / "worker_health.json"
        self.assertTrue(health_path.exists())

        data = json.loads(health_path.read_text())
        self.assertIn("T2", data)
        entry = data["T2"]
        self.assertEqual(entry["dispatch_id"], "dispatch-health-001")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["events"], 10)
        self.assertEqual(entry["last_tool"], "Read")

    def test_health_json_merges_multiple_terminals(self):
        """Multiple monitors write to the same file without overwriting each other."""
        m1 = WorkerHealthMonitor("T1", "dispatch-m1", events_dir=self._events_dir)
        m2 = WorkerHealthMonitor("T2", "dispatch-m2", events_dir=self._events_dir)

        m1.update(_make_tool_event("Edit"))
        m2.update(_make_tool_event("Bash"))
        m1.mark_completed()
        m2.mark_completed()

        health_path = self._events_dir / "worker_health.json"
        data = json.loads(health_path.read_text())

        self.assertIn("T1", data)
        self.assertIn("T2", data)
        self.assertEqual(data["T1"]["last_tool"], "Edit")
        self.assertEqual(data["T2"]["last_tool"], "Bash")

    def test_health_dict_format(self):
        """WorkerHealth.to_dict() produces expected keys."""
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-fmt-001", events_dir=self._events_dir
        )
        monitor.stop()
        monitor.update(_make_tool_event("Write"))

        h = monitor.health_status()
        d = h.to_dict()

        self.assertIn("dispatch_id", d)
        self.assertIn("status", d)
        self.assertIn("events", d)
        self.assertIn("elapsed", d)
        self.assertIn("last_tool", d)
        self.assertIn("estimated_progress", d)
        # elapsed should be formatted as NmNNs
        self.assertRegex(d["elapsed"], r"^\d+m\d{2}s$")


if __name__ == "__main__":
    unittest.main()
