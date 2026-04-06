#!/usr/bin/env python3
"""Tests for SubprocessAdapter — F28 PR-2.

Covers:
  1. spawn() stores config and returns success
  2. deliver() constructs correct CLI command (model, instruction)
  3. deliver() replaces active process for same terminal
  4. stop() sends SIGTERM; escalates to SIGKILL on TimeoutExpired
  5. stop() on unknown terminal returns success (idempotent)
  6. health() reports process alive vs. exited
  7. observe() reflects process state
  8. session_health() aggregates across terminal IDs
  9. shutdown() stops all processes
 10. capabilities() includes all REQUIRED_CAPABILITIES
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from adapter_types import (
    REQUIRED_CAPABILITIES,
    DeliveryResult,
    HealthResult,
    ObservationResult,
    SessionHealthResult,
    SpawnResult,
    StopResult,
)
from subprocess_adapter import SubprocessAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alive_process(pid: int = 12345) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = None       # alive
    proc.returncode = None
    return proc


def _make_dead_process(pid: int = 12345, returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = returncode
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------

class TestSpawn:
    def test_spawn_registers_config_and_succeeds(self):
        adapter = SubprocessAdapter()
        result = adapter.spawn("T1", {"model": "sonnet", "instruction": "do work"})
        assert isinstance(result, SpawnResult)
        assert result.success is True
        assert result.transport_ref == "subprocess:T1"
        assert adapter._configs["T1"]["model"] == "sonnet"

    def test_spawn_idempotent(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {"model": "opus"})
        r2 = adapter.spawn("T1", {"model": "haiku"})
        assert r2.success is True
        # Config updated to latest
        assert adapter._configs["T1"]["model"] == "haiku"


# ---------------------------------------------------------------------------
# deliver()
# ---------------------------------------------------------------------------

class TestDeliver:
    def test_deliver_constructs_correct_command(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {"model": "opus", "instruction": "run task"})

        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = adapter.deliver("T1", "dispatch-001")

        assert result.success is True
        assert result.terminal_id == "T1"
        assert result.dispatch_id == "dispatch-001"
        assert result.path_used == "subprocess"

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--model" in cmd
        assert "opus" in cmd

    def test_deliver_uses_default_model_when_no_config(self):
        adapter = SubprocessAdapter()
        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T2", "dispatch-002")

        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "sonnet"

    def test_deliver_overrides_model_via_kwarg(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {"model": "opus"})
        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T1", "dispatch-003", model="haiku")

        cmd = mock_popen.call_args[0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "haiku"

    def test_deliver_overrides_instruction_via_kwarg(self):
        adapter = SubprocessAdapter()
        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T1", "dispatch-004", instruction="custom task")

        cmd = mock_popen.call_args[0][0]
        assert "custom task" in cmd

    def test_deliver_tracks_process(self):
        adapter = SubprocessAdapter()
        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc):
            adapter.deliver("T1", "dispatch-005")
        assert adapter._processes["T1"] is mock_proc

    def test_deliver_replaces_existing_running_process(self):
        adapter = SubprocessAdapter()
        old_proc = _make_alive_process(pid=1111)
        adapter._processes["T1"] = old_proc

        new_proc = _make_alive_process(pid=2222)
        with patch("subprocess.Popen", return_value=new_proc):
            with patch("os.killpg") as mock_kill:
                with patch("os.getpgid", return_value=1111):
                    adapter.deliver("T1", "dispatch-006")

        mock_kill.assert_called_once_with(1111, signal.SIGTERM)
        assert adapter._processes["T1"] is new_proc

    def test_deliver_returns_failure_when_claude_not_found(self):
        adapter = SubprocessAdapter()
        with patch("subprocess.Popen", side_effect=FileNotFoundError("claude not found")):
            result = adapter.deliver("T1", "dispatch-007")

        assert result.success is False
        assert "claude not found" in result.failure_reason
        assert result.path_used == "none"

    def test_deliver_uses_setsid(self):
        adapter = SubprocessAdapter()
        mock_proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T1", "dispatch-008")

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("preexec_fn") is os.setsid


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_unknown_terminal_succeeds_idempotent(self):
        adapter = SubprocessAdapter()
        result = adapter.stop("T9")
        assert result.success is True
        assert result.was_running is False

    def test_stop_alive_process_sends_sigterm(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process(pid=9999)
        adapter._processes["T1"] = proc

        with patch("os.getpgid", return_value=9999):
            with patch("os.killpg") as mock_kill:
                result = adapter.stop("T1")

        mock_kill.assert_called_once_with(9999, signal.SIGTERM)
        assert result.success is True
        assert result.was_running is True

    def test_stop_escalates_to_sigkill_on_timeout(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process(pid=7777)
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="claude", timeout=10), None]
        adapter._processes["T1"] = proc

        with patch("os.getpgid", return_value=7777):
            with patch("os.killpg") as mock_kill:
                result = adapter.stop("T1")

        assert mock_kill.call_args_list == [
            call(7777, signal.SIGTERM),
            call(7777, signal.SIGKILL),
        ]
        assert result.success is True

    def test_stop_handles_missing_process_group(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process(pid=8888)
        adapter._processes["T1"] = proc

        with patch("os.getpgid", side_effect=OSError("no such process")):
            result = adapter.stop("T1")

        assert result.success is True

    def test_stop_removes_process_from_tracking(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        adapter._processes["T1"] = proc

        with patch("os.getpgid", return_value=proc.pid):
            with patch("os.killpg"):
                adapter.stop("T1")

        assert "T1" not in adapter._processes

    def test_stop_dead_process_reports_was_running_false(self):
        adapter = SubprocessAdapter()
        proc = _make_dead_process(returncode=0)
        adapter._processes["T1"] = proc

        result = adapter.stop("T1")
        assert result.success is True
        assert result.was_running is False


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_unknown_terminal_unhealthy(self):
        adapter = SubprocessAdapter()
        result = adapter.health("T9")
        assert isinstance(result, HealthResult)
        assert result.healthy is False
        assert result.surface_exists is False
        assert result.process_alive is False

    def test_health_spawned_but_not_delivered_surface_exists(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {})
        result = adapter.health("T1")
        assert result.surface_exists is True
        assert result.process_alive is False
        assert result.healthy is False

    def test_health_alive_process_is_healthy(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process(pid=1234)
        adapter._processes["T1"] = proc

        result = adapter.health("T1")
        assert result.healthy is True
        assert result.surface_exists is True
        assert result.process_alive is True
        assert result.details["pid"] == 1234

    def test_health_dead_process_unhealthy(self):
        adapter = SubprocessAdapter()
        proc = _make_dead_process(pid=1234, returncode=1)
        adapter._processes["T1"] = proc

        result = adapter.health("T1")
        assert result.healthy is False
        assert result.process_alive is False


# ---------------------------------------------------------------------------
# observe()
# ---------------------------------------------------------------------------

class TestObserve:
    def test_observe_unknown_terminal(self):
        adapter = SubprocessAdapter()
        result = adapter.observe("T9")
        assert isinstance(result, ObservationResult)
        assert result.exists is False
        assert result.responsive is False

    def test_observe_alive_process(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process(pid=5555)
        adapter._processes["T1"] = proc

        result = adapter.observe("T1")
        assert result.exists is True
        assert result.responsive is True
        assert result.transport_state["pid"] == 5555
        assert result.transport_state["process_alive"] is True

    def test_observe_dead_process(self):
        adapter = SubprocessAdapter()
        proc = _make_dead_process(pid=5555, returncode=0)
        adapter._processes["T1"] = proc

        result = adapter.observe("T1")
        assert result.exists is True
        assert result.responsive is False

    def test_observe_spawned_not_delivered(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {})
        result = adapter.observe("T1")
        assert result.exists is True
        assert result.responsive is False


# ---------------------------------------------------------------------------
# session_health()
# ---------------------------------------------------------------------------

class TestSessionHealth:
    def test_session_health_all_unknown(self):
        adapter = SubprocessAdapter()
        result = adapter.session_health(["T1", "T2", "T3"])
        assert isinstance(result, SessionHealthResult)
        assert result.session_exists is False
        assert set(result.degraded_terminals) == {"T1", "T2", "T3"}

    def test_session_health_one_alive(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _make_alive_process(pid=100)
        result = adapter.session_health(["T1", "T2"])
        assert result.session_exists is True
        assert "T1" not in result.degraded_terminals
        assert "T2" in result.degraded_terminals

    def test_session_health_returns_per_terminal_results(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _make_alive_process(pid=101)
        result = adapter.session_health(["T1", "T2"])
        assert result.terminals["T1"].healthy is True
        assert result.terminals["T2"].healthy is False


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_stops_all_tracked_processes(self):
        adapter = SubprocessAdapter()
        procs = {
            "T1": _make_alive_process(pid=1001),
            "T2": _make_alive_process(pid=1002),
        }
        adapter._processes.update(procs)

        with patch("os.getpgid", side_effect=lambda pid: pid):
            with patch("os.killpg"):
                adapter.shutdown()

        assert len(adapter._processes) == 0

    def test_shutdown_graceful_false_still_stops(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _make_alive_process(pid=9001)

        with patch("os.getpgid", return_value=9001):
            with patch("os.killpg"):
                adapter.shutdown(graceful=False)

        assert "T1" not in adapter._processes


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_includes_all_required_capabilities(self):
        adapter = SubprocessAdapter()
        caps = adapter.capabilities()
        missing = [c for c in REQUIRED_CAPABILITIES if c not in caps]
        assert missing == [], f"Missing required capabilities: {missing}"

    def test_adapter_type(self):
        adapter = SubprocessAdapter()
        assert adapter.adapter_type() == "subprocess"
