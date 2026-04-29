#!/usr/bin/env python3
"""Tests for T5-PR4: WorkerHealthMonitor → EventStore integration.

Gate: t5-pr4-worker-health-eventstore
Covers:
  - Case A: stuck transition → event_store.append called with type=worker_stuck, correct fields
  - Case B: NOT stuck → no event_store call
  - Case C: multiple stuck transitions → counter increments, multiple events appended
  - Case D: event_store None → only logger.warning, no crash
  - Case E: receipt includes stuck_event_count (0 when no stuck, N when N stuck events)
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from worker_health_monitor import (
    WorkerHealthMonitor,
    HealthStatus,
    SLOW_THRESHOLD,
)


def _force_stuck(monitor: WorkerHealthMonitor) -> None:
    """Force the monitor into STUCK status by backdating last_event_time."""
    with monitor._lock:
        monitor._last_event_time = time.monotonic() - (SLOW_THRESHOLD + 10)


class TestCaseA_StuckTransitionPersistsToEventStore(unittest.TestCase):
    """Case A: stuck transition → event_store.append called with correct fields."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_event_store_append_called_on_stuck(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-A1",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)

        stuck_log = self._events_dir / "worker_stuck.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        mock_store.append.assert_called_once()
        args, kwargs = mock_store.append.call_args
        terminal_arg = args[0]
        event_arg = args[1]
        self.assertEqual(terminal_arg, "T1")
        self.assertEqual(event_arg["type"], "worker_stuck")
        self.assertEqual(event_arg["dispatch_id"], "dispatch-A1")
        self.assertIn("elapsed_secs", event_arg)
        self.assertIn("last_event_type", event_arg)
        self.assertEqual(kwargs.get("dispatch_id"), "dispatch-A1")

    def test_event_store_receives_last_event_type(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-A2",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        monitor.update({"type": "tool_use", "data": {"name": "Edit"}})
        _force_stuck(monitor)

        stuck_log = self._events_dir / "worker_stuck_a2.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        _, call_args = mock_store.append.call_args
        event_arg = mock_store.append.call_args[0][1]
        self.assertEqual(event_arg["last_event_type"], "tool_use")

    def test_elapsed_secs_is_positive(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-A3",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)

        stuck_log = self._events_dir / "worker_stuck_a3.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        event_arg = mock_store.append.call_args[0][1]
        self.assertGreater(event_arg["elapsed_secs"], 0)


class TestCaseB_NotStuckNoEventStoreCall(unittest.TestCase):
    """Case B: NOT stuck → no event_store call."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_append_when_active(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-B1",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        monitor.update({"type": "text", "data": {}})  # recent event → ACTIVE

        stuck_log = self._events_dir / "worker_stuck_b1.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        mock_store.append.assert_not_called()
        self.assertFalse(stuck_log.exists())

    def test_no_append_when_slow_not_stuck(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-B2",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        with monitor._lock:
            # 90s silence → SLOW (not STUCK)
            from worker_health_monitor import ACTIVE_THRESHOLD
            monitor._last_event_time = time.monotonic() - (ACTIVE_THRESHOLD + 30)

        stuck_log = self._events_dir / "worker_stuck_b2.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        mock_store.append.assert_not_called()


class TestCaseC_MultipleStuckTransitions(unittest.TestCase):
    """Case C: multiple stuck transitions → counter increments, multiple events appended."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_stuck_count_increments_each_call(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-C1",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_c1.ndjson"

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        self.assertEqual(monitor.stuck_count, 1)

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        self.assertEqual(monitor.stuck_count, 2)

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        self.assertEqual(monitor.stuck_count, 3)

    def test_event_store_append_called_for_each_stuck_transition(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-C2",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_c2.ndjson"

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        monitor.log_stuck_event(stuck_log_path=stuck_log)
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertEqual(mock_store.append.call_count, 3)

    def test_ndjson_log_has_multiple_entries(self):
        import json
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-C3",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_c3.ndjson"

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        lines = [l for l in stuck_log.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            entry = json.loads(line)
            self.assertEqual(entry["dispatch_id"], "dispatch-C3")


class TestCaseD_EventStoreNone(unittest.TestCase):
    """Case D: event_store None → only logger.warning, no crash."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_crash_when_event_store_is_none(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-D1",
            events_dir=self._events_dir,
            event_store=None,
        )
        monitor.stop()
        _force_stuck(monitor)

        stuck_log = self._events_dir / "worker_stuck_d1.ndjson"
        # Must not raise
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertTrue(stuck_log.exists())
        self.assertEqual(monitor.stuck_count, 1)

    def test_warning_logged_when_event_store_none(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-D2",
            events_dir=self._events_dir,
            event_store=None,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_d2.ndjson"

        with patch("worker_health_monitor.logger") as mock_logger:
            monitor.log_stuck_event(stuck_log_path=stuck_log)
            mock_logger.warning.assert_called_once()

    def test_stuck_count_increments_even_without_event_store(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-D3",
            events_dir=self._events_dir,
            event_store=None,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_d3.ndjson"

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertEqual(monitor.stuck_count, 2)


class TestCaseE_StuckCountOnReceipt(unittest.TestCase):
    """Case E: stuck_count accessor returns correct values."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._events_dir = Path(self._tmpdir) / "events"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_stuck_count_zero_when_no_stuck_events(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-E1",
            events_dir=self._events_dir,
        )
        monitor.stop()
        monitor.update({"type": "text", "data": {}})

        self.assertEqual(monitor.stuck_count, 0)

    def test_stuck_count_reflects_n_stuck_transitions(self):
        mock_store = MagicMock()
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-E2",
            events_dir=self._events_dir,
            event_store=mock_store,
        )
        monitor.stop()
        _force_stuck(monitor)
        stuck_log = self._events_dir / "worker_stuck_e2.ndjson"

        monitor.log_stuck_event(stuck_log_path=stuck_log)
        monitor.log_stuck_event(stuck_log_path=stuck_log)

        self.assertEqual(monitor.stuck_count, 2)

    def test_stuck_count_not_incremented_by_non_stuck_log_call(self):
        monitor = WorkerHealthMonitor(
            "T1", "dispatch-E3",
            events_dir=self._events_dir,
        )
        monitor.stop()
        monitor.update({"type": "text", "data": {}})  # ACTIVE

        stuck_log = self._events_dir / "worker_stuck_e3.ndjson"
        monitor.log_stuck_event(stuck_log_path=stuck_log)  # ACTIVE → early return

        self.assertEqual(monitor.stuck_count, 0)


if __name__ == "__main__":
    unittest.main()
