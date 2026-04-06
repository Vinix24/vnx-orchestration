#!/usr/bin/env python3
"""Tests for read_events_with_timeout() and lease heartbeat — F31 PR-0.

Covers:
  1. chunk timeout kills subprocess when no output is produced
  2. total deadline kills subprocess during slow output
  3. normal completion yields all events without timeout
  4. heartbeat thread renews lease at interval
  5. heartbeat thread stops cleanly on delivery completion
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from subprocess_adapter import StreamEvent, SubprocessAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipe_process(lines: list[bytes], pid: int = 99999) -> MagicMock:
    """Create a mock process whose stdout is a real file descriptor (via os.pipe).

    Writes *lines* into the pipe's write end, then closes it so readline()
    returns EOF after the last line.
    """
    r_fd, w_fd = os.pipe()

    # Write all lines into the pipe, then close write end → EOF
    with os.fdopen(w_fd, "wb") as w:
        for line in lines:
            w.write(line)

    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout = os.fdopen(r_fd, "rb")
    return proc


def _make_blocking_pipe_process(pid: int = 99999) -> tuple[MagicMock, int]:
    """Create a mock process whose stdout blocks forever (write end stays open).

    Returns (process_mock, write_fd) so the caller can close write_fd to unblock.
    """
    r_fd, w_fd = os.pipe()
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout = os.fdopen(r_fd, "rb")
    return proc, w_fd


def _event_line(etype: str, **data: object) -> bytes:
    """Build a JSON line as bytes for a stream event."""
    payload = {"type": etype, **data}
    return json.dumps(payload).encode() + b"\n"


# ---------------------------------------------------------------------------
# Tests — read_events_with_timeout
# ---------------------------------------------------------------------------


class TestChunkTimeout:
    def test_chunk_timeout_kills_process(self):
        """No output for chunk_timeout seconds -> subprocess killed."""
        adapter = SubprocessAdapter()
        proc, w_fd = _make_blocking_pipe_process()
        adapter._processes["T1"] = proc

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=0.2, total_deadline=5.0,
        ))

        assert events == []
        # Process should have been stopped (removed from _processes)
        assert "T1" not in adapter._processes

        # Clean up write fd
        os.close(w_fd)


class TestTotalDeadline:
    def test_total_deadline_kills_process(self):
        """Slow trickle of output -> total deadline exceeded -> subprocess killed."""
        adapter = SubprocessAdapter()

        # We'll use a pipe where we drip events from a background thread
        r_fd, w_fd = os.pipe()
        proc = MagicMock()
        proc.pid = 88888
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        # Background thread: drip one event every 0.15s
        def drip():
            try:
                for i in range(50):  # more than enough to exceed deadline
                    line = _event_line("text", text=f"msg-{i}")
                    os.write(w_fd, line)
                    time.sleep(0.15)
            except OSError:
                pass  # pipe closed by stop()
            finally:
                try:
                    os.close(w_fd)
                except OSError:
                    pass

        t = threading.Thread(target=drip, daemon=True)
        t.start()

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=2.0, total_deadline=0.5,
        ))

        # Should have gotten some events but not all 50
        assert len(events) > 0
        assert len(events) < 50
        assert "T1" not in adapter._processes

        t.join(timeout=2)


class TestNormalCompletion:
    def test_normal_completion_no_timeout(self):
        """Process completes normally -> all events yielded, no timeout."""
        lines = [
            _event_line("init", session_id="sess-1"),
            _event_line("text", text="hello"),
            _event_line("result", result="done", session_id="sess-1"),
        ]
        adapter = SubprocessAdapter()
        proc = _make_pipe_process(lines)
        adapter._processes["T1"] = proc

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=5.0, total_deadline=10.0,
        ))

        types = [e.type for e in events]
        assert "init" in types
        assert "text" in types
        assert "result" in types

    def test_no_process_returns_empty(self):
        """No process registered -> empty iterator."""
        adapter = SubprocessAdapter()
        events = list(adapter.read_events_with_timeout("MISSING"))
        assert events == []


# ---------------------------------------------------------------------------
# Tests — heartbeat thread
# ---------------------------------------------------------------------------


class TestHeartbeatThread:
    @patch("lease_manager.LeaseManager", autospec=False)
    def test_heartbeat_thread_renews_lease(self, MockLM):
        """Heartbeat thread calls renew() at the configured interval."""
        from subprocess_dispatch import _heartbeat_loop

        mock_lm_instance = MagicMock()
        MockLM.return_value = mock_lm_instance

        stop_event = threading.Event()
        state_dir = Path("/tmp/test-state")

        t = threading.Thread(
            target=_heartbeat_loop,
            args=("T1", "d-001", 5, stop_event, state_dir),
            kwargs={"interval": 0.1},
            daemon=True,
        )
        t.start()

        # Let it run for ~0.35s (should fire ~3 times at 0.1s interval)
        time.sleep(0.35)
        stop_event.set()
        t.join(timeout=2)

        # Should have called renew at least twice
        assert mock_lm_instance.renew.call_count >= 2
        # Verify correct arguments
        for c in mock_lm_instance.renew.call_args_list:
            assert c == call("T1", generation=5, actor="heartbeat")

    @patch("lease_manager.LeaseManager", autospec=False)
    def test_heartbeat_stops_on_completion(self, MockLM):
        """Heartbeat thread stops when stop_event is set."""
        from subprocess_dispatch import _heartbeat_loop

        mock_lm_instance = MagicMock()
        MockLM.return_value = mock_lm_instance

        stop_event = threading.Event()
        state_dir = Path("/tmp/test-state")

        t = threading.Thread(
            target=_heartbeat_loop,
            args=("T1", "d-002", 3, stop_event, state_dir),
            kwargs={"interval": 10.0},  # long interval — should not fire
            daemon=True,
        )
        t.start()

        # Stop immediately
        stop_event.set()
        t.join(timeout=2)

        assert not t.is_alive()
        # With 10s interval and immediate stop, renew should not have been called
        assert mock_lm_instance.renew.call_count == 0

    @patch("lease_manager.LeaseManager", autospec=False)
    def test_heartbeat_survives_renew_failure(self, MockLM):
        """Heartbeat continues even if renew() raises."""
        from subprocess_dispatch import _heartbeat_loop

        mock_lm_instance = MagicMock()
        mock_lm_instance.renew.side_effect = RuntimeError("db locked")
        MockLM.return_value = mock_lm_instance

        stop_event = threading.Event()

        t = threading.Thread(
            target=_heartbeat_loop,
            args=("T1", "d-003", 1, stop_event, Path("/tmp")),
            kwargs={"interval": 0.05},
            daemon=True,
        )
        t.start()

        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=2)

        # Should have attempted renew multiple times despite failures
        assert mock_lm_instance.renew.call_count >= 2
