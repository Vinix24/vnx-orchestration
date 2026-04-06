#!/usr/bin/env python3
"""Tests for subprocess health monitor, deliver_with_recovery, and receipt writing.

Covers:
  - Worker classification (healthy, stalled, dead, hung)
  - Receipt writing on success and failure
  - Retry logic with backoff
  - Budget exhaustion
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import pytest

# Ensure scripts/lib is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


# ---------------------------------------------------------------------------
# Lightweight stubs (avoid importing full adapter / supervisor stacks)
# ---------------------------------------------------------------------------

@dataclass
class FakeHealthResult:
    healthy: bool
    surface_exists: bool
    process_alive: bool
    details: Dict[str, Any]


# ---------------------------------------------------------------------------
# Import after path setup
# ---------------------------------------------------------------------------

from subprocess_health_monitor import (
    SubprocessHealthMonitor,
    WorkerClassification,
    WorkerInfo,
    classify_worker,
    HUNG_THRESHOLD,
    STALL_THRESHOLD,
)


# ===================================================================
# classify_worker tests
# ===================================================================


class TestClassifyWorker:
    """Test worker classification logic."""

    def test_healthy_worker(self):
        """Process alive and recent events → healthy."""
        health = FakeHealthResult(
            healthy=True, surface_exists=True, process_alive=True,
            details={"pid": 1234},
        )
        worker = WorkerInfo(terminal_id="T1", dispatch_id="d1")
        worker.last_event_at = time.time() - 5  # 5 seconds ago
        assert classify_worker(health, worker, time.time()) == WorkerClassification.HEALTHY

    def test_dead_worker(self):
        """Process exited → dead regardless of event timing."""
        health = FakeHealthResult(
            healthy=False, surface_exists=True, process_alive=False,
            details={"pid": 1234, "returncode": 1},
        )
        worker = WorkerInfo(terminal_id="T1", dispatch_id="d1")
        assert classify_worker(health, worker, time.time()) == WorkerClassification.DEAD

    def test_stalled_worker(self):
        """Process alive but no events for >120s → stalled."""
        health = FakeHealthResult(
            healthy=True, surface_exists=True, process_alive=True,
            details={"pid": 1234},
        )
        worker = WorkerInfo(terminal_id="T1", dispatch_id="d1")
        worker.last_event_at = time.time() - (STALL_THRESHOLD + 10)
        assert classify_worker(health, worker, time.time()) == WorkerClassification.STALLED

    def test_hung_worker(self):
        """Process alive but no output for >300s → hung."""
        health = FakeHealthResult(
            healthy=True, surface_exists=True, process_alive=True,
            details={"pid": 1234},
        )
        worker = WorkerInfo(terminal_id="T1", dispatch_id="d1")
        worker.last_event_at = time.time() - (HUNG_THRESHOLD + 10)
        assert classify_worker(health, worker, time.time()) == WorkerClassification.HUNG

    def test_dead_takes_priority_over_hung(self):
        """Dead classification takes precedence even if event timing suggests hung."""
        health = FakeHealthResult(
            healthy=False, surface_exists=True, process_alive=False,
            details={"pid": 1234, "returncode": -9},
        )
        worker = WorkerInfo(terminal_id="T1", dispatch_id="d1")
        worker.last_event_at = time.time() - (HUNG_THRESHOLD + 100)
        assert classify_worker(health, worker, time.time()) == WorkerClassification.DEAD


# ===================================================================
# Receipt tests
# ===================================================================


class TestReceipts:
    """Test receipt writing on delivery success/failure."""

    def test_receipt_written_on_success(self, tmp_path):
        """deliver_with_recovery writes status=done receipt on success."""
        from subprocess_dispatch import _write_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir):
            _write_receipt("dispatch-1", "T1", "done", attempt=0)

        receipt_path = state_dir / "t0_receipts.ndjson"
        assert receipt_path.exists()

        lines = receipt_path.read_text().strip().split("\n")
        assert len(lines) == 1
        receipt = json.loads(lines[0])
        assert receipt["dispatch_id"] == "dispatch-1"
        assert receipt["terminal"] == "T1"
        assert receipt["status"] == "done"
        assert receipt["source"] == "subprocess"
        assert receipt["event_type"] == "subprocess_completion"
        assert receipt["attempt"] == 0

    def test_receipt_written_on_failure(self, tmp_path):
        """_write_receipt writes status=failed receipt with failure reason."""
        from subprocess_dispatch import _write_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir):
            _write_receipt(
                "dispatch-2", "T2", "failed",
                attempt=3, failure_reason="Exhausted 3 retries",
            )

        receipt_path = state_dir / "t0_receipts.ndjson"
        lines = receipt_path.read_text().strip().split("\n")
        receipt = json.loads(lines[0])
        assert receipt["status"] == "failed"
        assert receipt["failure_reason"] == "Exhausted 3 retries"
        assert receipt["attempt"] == 3


# ===================================================================
# deliver_with_recovery tests
# ===================================================================


class TestDeliverWithRecovery:
    """Test retry logic and receipt integration."""

    def test_success_on_first_attempt(self, tmp_path):
        """First attempt succeeds → done receipt, returns True."""
        from subprocess_dispatch import deliver_with_recovery

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with (
            mock.patch("subprocess_dispatch.deliver_via_subprocess", return_value=True) as mock_deliver,
            mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir),
        ):
            result = deliver_with_recovery("T1", "do work", "sonnet", "d1", max_retries=3)

        assert result is True
        assert mock_deliver.call_count == 1

        receipt_path = state_dir / "t0_receipts.ndjson"
        receipt = json.loads(receipt_path.read_text().strip())
        assert receipt["status"] == "done"
        assert receipt["attempt"] == 0

    def test_retry_on_failure_then_success(self, tmp_path):
        """First attempt fails, second succeeds → done receipt."""
        from subprocess_dispatch import deliver_with_recovery

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with (
            mock.patch(
                "subprocess_dispatch.deliver_via_subprocess",
                side_effect=[False, True],
            ) as mock_deliver,
            mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir),
            mock.patch("subprocess_dispatch.time.sleep") as mock_sleep,
        ):
            result = deliver_with_recovery("T1", "do work", "sonnet", "d1", max_retries=3)

        assert result is True
        assert mock_deliver.call_count == 2
        # First retry backoff: 30s * 2^0 = 30s
        mock_sleep.assert_called_once_with(30)

        receipt = json.loads((state_dir / "t0_receipts.ndjson").read_text().strip())
        assert receipt["status"] == "done"
        assert receipt["attempt"] == 1

    def test_max_retries_exhausted(self, tmp_path):
        """All attempts fail → failed receipt, returns False."""
        from subprocess_dispatch import deliver_with_recovery

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with (
            mock.patch("subprocess_dispatch.deliver_via_subprocess", return_value=False) as mock_deliver,
            mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir),
            mock.patch("subprocess_dispatch.time.sleep"),
        ):
            result = deliver_with_recovery("T1", "do work", "sonnet", "d1", max_retries=2)

        assert result is False
        # initial + 2 retries = 3 calls
        assert mock_deliver.call_count == 3

        receipt = json.loads((state_dir / "t0_receipts.ndjson").read_text().strip())
        assert receipt["status"] == "failed"
        assert receipt["attempt"] == 2
        assert "Exhausted 2 retries" in receipt["failure_reason"]

    def test_exponential_backoff(self, tmp_path):
        """Verify backoff doubles: 30s, 60s, 120s."""
        from subprocess_dispatch import deliver_with_recovery

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with (
            mock.patch("subprocess_dispatch.deliver_via_subprocess", return_value=False),
            mock.patch("subprocess_dispatch._default_state_dir", return_value=state_dir),
            mock.patch("subprocess_dispatch.time.sleep") as mock_sleep,
        ):
            deliver_with_recovery("T1", "do work", "sonnet", "d1", max_retries=3)

        assert mock_sleep.call_args_list == [
            mock.call(30),
            mock.call(60),
            mock.call(120),
        ]


# ===================================================================
# Health monitor integration tests
# ===================================================================


class TestHealthMonitor:
    """Test SubprocessHealthMonitor register/check/incident flow."""

    def _make_adapter(self, health_map: Dict[str, FakeHealthResult]):
        """Create a fake adapter with canned health() results."""
        adapter = mock.MagicMock()
        adapter.health.side_effect = lambda tid: health_map.get(
            tid,
            FakeHealthResult(healthy=False, surface_exists=False, process_alive=False, details={}),
        )
        return adapter

    def test_check_all_healthy(self, tmp_path):
        """All workers healthy → no incidents."""
        health_map = {
            "T1": FakeHealthResult(healthy=True, surface_exists=True, process_alive=True, details={"pid": 100}),
        }
        adapter = self._make_adapter(health_map)
        monitor = SubprocessHealthMonitor(
            adapter, state_dir=tmp_path, log_dir=tmp_path / "logs",
        )
        monitor.register("T1", "d1", pid=100)
        results = monitor.check_all()
        assert results["T1"] == WorkerClassification.HEALTHY

    def test_check_all_dead_triggers_incident(self, tmp_path):
        """Dead worker → PROCESS_CRASH incident via supervisor."""
        health_map = {
            "T1": FakeHealthResult(healthy=False, surface_exists=True, process_alive=False, details={"pid": 100, "returncode": 1}),
        }
        adapter = self._make_adapter(health_map)
        monitor = SubprocessHealthMonitor(
            adapter, state_dir=tmp_path, log_dir=tmp_path / "logs",
        )
        monitor.register("T1", "d1", pid=100)

        fake_supervisor = mock.MagicMock()
        monitor._supervisor = fake_supervisor

        results = monitor.check_all()
        assert results["T1"] == WorkerClassification.DEAD
        fake_supervisor.handle_incident.assert_called_once()
        call_kwargs = fake_supervisor.handle_incident.call_args[1]
        assert call_kwargs["incident_class"].value == "process_crash"
        assert call_kwargs["terminal_id"] == "T1"

    def test_check_all_hung_triggers_incident(self, tmp_path):
        """Hung worker → TERMINAL_UNRESPONSIVE incident."""
        health_map = {
            "T1": FakeHealthResult(healthy=True, surface_exists=True, process_alive=True, details={"pid": 100}),
        }
        adapter = self._make_adapter(health_map)
        monitor = SubprocessHealthMonitor(
            adapter, state_dir=tmp_path, log_dir=tmp_path / "logs",
        )
        monitor.register("T1", "d1", pid=100)

        # Simulate hung: last event was 400s ago
        with monitor._lock:
            monitor._workers["T1"].last_event_at = time.time() - 400

        fake_supervisor = mock.MagicMock()
        monitor._supervisor = fake_supervisor

        results = monitor.check_all()
        assert results["T1"] == WorkerClassification.HUNG
        call_kwargs = fake_supervisor.handle_incident.call_args[1]
        assert call_kwargs["incident_class"].value == "terminal_unresponsive"

    def test_register_unregister(self, tmp_path):
        """Workers can be registered and unregistered."""
        adapter = mock.MagicMock()
        monitor = SubprocessHealthMonitor(
            adapter, state_dir=tmp_path, log_dir=tmp_path / "logs",
        )
        monitor.register("T1", "d1")
        assert "T1" in monitor.get_workers()
        monitor.unregister("T1")
        assert "T1" not in monitor.get_workers()

    def test_health_log_written(self, tmp_path):
        """Health checks write to subprocess_health.log."""
        health_map = {
            "T1": FakeHealthResult(healthy=True, surface_exists=True, process_alive=True, details={"pid": 100}),
        }
        adapter = self._make_adapter(health_map)
        log_dir = tmp_path / "logs"
        monitor = SubprocessHealthMonitor(
            adapter, state_dir=tmp_path, log_dir=log_dir,
        )
        monitor.register("T1", "d1", pid=100)
        monitor.check_all()

        log_path = log_dir / "subprocess_health.log"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["terminal_id"] == "T1"
        assert entry["classification"] == "healthy"
