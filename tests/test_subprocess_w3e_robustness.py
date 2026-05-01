#!/usr/bin/env python3
"""Regression tests for W3E subprocess delivery robustness fixes.

OI-1120 — timeout-success misclassification:
    Verifies that non-zero returncode cached before stop() removes the process
    is used in _classify_completion to fail-close the dispatch.

OI-1122 — SIGTERM without SIGKILL fallback:
    Verifies that stop() sends SIGKILL after SIGTERM timeout and does not raise
    when the SIGKILL wait also times out.

OI-1123 — readline() blocks after select():
    Verifies that chunk_timeout is enforced even when select() returns ready
    but the available data contains no newline (partial line scenario).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
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

def _make_pipe_proc(lines: list[bytes], returncode: int = 0, pid: int = 99999) -> MagicMock:
    """Pipe-backed mock process with real fd for select() compatibility."""
    r_fd, w_fd = os.pipe()
    with os.fdopen(w_fd, "wb") as w:
        for line in lines:
            w.write(line)

    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = returncode
    proc.returncode = returncode
    proc.stdout = os.fdopen(r_fd, "rb")
    return proc


def _blocking_pipe_proc(pid: int = 99999) -> tuple[MagicMock, int]:
    """Pipe where write end stays open — read blocks until we close w_fd."""
    r_fd, w_fd = os.pipe()
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout = os.fdopen(r_fd, "rb")
    return proc, w_fd


def _json_line(*args, **kwargs) -> bytes:
    return json.dumps(dict(*args, **kwargs)).encode() + b"\n"


# ---------------------------------------------------------------------------
# OI-1120 — returncode caching and _classify_completion
# ---------------------------------------------------------------------------

class TestOI1120ReturncodeCaching:
    """stop() caches final returncode; _classify_completion reads the cache."""

    def test_stop_caches_nonzero_returncode(self):
        """After stop(), _returncode_cache contains the process exit code."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 11111
        proc.poll.side_effect = [None, 1]  # first: running; second: after kill
        adapter._processes["T1"] = proc

        with patch("subprocess_adapter.os.killpg"), \
             patch("subprocess_adapter.os.getpgid", return_value=11111):
            adapter.stop("T1")

        assert adapter._returncode_cache.get("T1") == 1

    def test_stop_caches_zero_returncode(self):
        """stop() caches returncode 0 (clean exit killed by stop)."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 22222
        proc.poll.side_effect = [None, 0]
        adapter._processes["T1"] = proc

        with patch("subprocess_adapter.os.killpg"), \
             patch("subprocess_adapter.os.getpgid", return_value=22222):
            adapter.stop("T1")

        assert adapter._returncode_cache.get("T1") == 0

    def test_stop_skips_cache_when_process_already_dead(self):
        """If process already exited, stop() still caches returncode."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 33333
        proc.poll.return_value = 2  # already dead
        adapter._processes["T1"] = proc

        # poll() returns non-None so was_running=False, but we still cache
        adapter.stop("T1")

        assert adapter._returncode_cache.get("T1") == 2

    def test_classify_completion_uses_cached_returncode(self):
        """When observe() has no returncode, _classify_completion falls back to cache."""
        from subprocess_dispatch_internals.delivery import _classify_completion

        adapter = MagicMock()
        adapter.was_timed_out.return_value = False
        obs = MagicMock()
        obs.transport_state = {}  # no returncode — process was removed by stop()
        adapter.observe.return_value = obs
        adapter._returncode_cache = {"T1": 1}  # non-zero cached by stop()

        with patch("subprocess_dispatch._promote_manifest", return_value=None):
            result = _classify_completion(
                adapter=adapter,
                terminal_id="T1",
                dispatch_id="d-oi1120",
                session_id=None,
                event_count=3,
                touched_files=set(),
                manifest_path=None,
                rotation_triggered=False,
                pending_handover=None,
            )

        assert result.success is False

    def test_classify_completion_timeout_is_failure_when_process_removed(self):
        """Timeout must fail even when process is removed (returncode=None from observe)."""
        from subprocess_dispatch_internals.delivery import _classify_completion

        adapter = MagicMock()
        adapter.was_timed_out.return_value = True  # timeout fired
        obs = MagicMock()
        obs.transport_state = {}  # process was popped by stop()
        adapter.observe.return_value = obs
        adapter._returncode_cache = {}  # nothing cached

        with patch("subprocess_dispatch._promote_manifest", return_value=None):
            result = _classify_completion(
                adapter=adapter,
                terminal_id="T1",
                dispatch_id="d-oi1120-timeout",
                session_id=None,
                event_count=0,
                touched_files=set(),
                manifest_path=None,
                rotation_triggered=False,
                pending_handover=None,
            )

        assert result.success is False

    def test_read_events_with_timeout_caches_returncode_at_eof(self):
        """EOF path captures process returncode into _returncode_cache."""
        lines = [
            _json_line(type="init", session_id="s1"),
            _json_line(type="result", result="done"),
        ]
        adapter = SubprocessAdapter()
        proc = _make_pipe_proc(lines, returncode=1)  # non-zero exit
        adapter._processes["T1"] = proc

        list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=10.0))

        assert adapter._returncode_cache.get("T1") == 1

    def test_read_events_with_timeout_caches_zero_returncode(self):
        """Zero returncode is also cached (confirms cache works for success path)."""
        lines = [_json_line(type="result", result="ok")]
        adapter = SubprocessAdapter()
        proc = _make_pipe_proc(lines, returncode=0)
        adapter._processes["T1"] = proc

        list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=10.0))

        assert adapter._returncode_cache.get("T1") == 0


# ---------------------------------------------------------------------------
# OI-1122 — SIGTERM without SIGKILL fallback
# ---------------------------------------------------------------------------

class TestOI1122SIGKILLFallback:
    """stop() must escalate to SIGKILL when SIGTERM is ignored."""

    def test_stop_sends_sigkill_when_sigterm_timeout(self):
        """stop() escalates to SIGKILL after SIGTERM + 10s wait times out."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 44444
        proc.poll.side_effect = [None, None]  # alive on first poll, then after kill

        # SIGTERM wait times out; SIGKILL wait succeeds
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=10), None]

        adapter._processes["T1"] = proc  # register process

        with patch("subprocess_adapter.os.killpg") as mock_killpg, \
             patch("subprocess_adapter.os.getpgid", return_value=44444):
            result = adapter.stop("T1")

        assert result.success is True
        mock_killpg.assert_any_call(44444, signal.SIGTERM)
        mock_killpg.assert_any_call(44444, signal.SIGKILL)

    def test_stop_does_not_raise_when_sigkill_wait_also_times_out(self):
        """OI-1122: stop() must not raise even when SIGKILL wait also times out."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 55555
        proc.poll.side_effect = [None, None]  # alive

        # Both wait() calls time out — process in D-state
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=10),
            subprocess.TimeoutExpired(cmd="x", timeout=5),
        ]

        adapter._processes["T1"] = proc  # register process

        with patch("subprocess_adapter.os.killpg"), \
             patch("subprocess_adapter.os.getpgid", return_value=55555):
            result = adapter.stop("T1")  # must not raise

        assert result.success is True

    def test_stop_process_already_dead_no_signals_sent(self):
        """stop() on an already-dead process skips signal sending."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 66666
        proc.poll.return_value = 0  # already dead

        adapter._processes["T1"] = proc  # register process

        with patch("subprocess_adapter.os.killpg") as mock_killpg, \
             patch("subprocess_adapter.os.getpgid", return_value=66666):
            result = adapter.stop("T1")

        assert result.success is True
        assert result.was_running is False
        mock_killpg.assert_not_called()

    def test_stop_tolerates_process_already_gone_oserror(self):
        """stop() handles OSError (process vanished between poll and killpg)."""
        adapter = SubprocessAdapter()
        proc = MagicMock()
        proc.pid = 77777
        proc.poll.return_value = None  # alive at poll time

        adapter._processes["T1"] = proc  # register process

        with patch("subprocess_adapter.os.killpg", side_effect=ProcessLookupError), \
             patch("subprocess_adapter.os.getpgid", return_value=77777):
            result = adapter.stop("T1")  # must not raise

        assert result.success is True


# ---------------------------------------------------------------------------
# OI-1123 — readline() blocks after select()
# ---------------------------------------------------------------------------

class TestOI1123NonBlockingRead:
    """chunk_timeout must fire even when select() returns ready on partial data."""

    def test_chunk_timeout_fires_after_partial_line_write(self):
        """OI-1123 regression: select() returns ready on partial data (no newline).

        With readline(): readline() blocks waiting for '\\n' — chunk_timeout bypassed.
        With os.read() + buffer: reads partial bytes, loops, next select() times out.
        """
        adapter = SubprocessAdapter()
        r_fd, w_fd = os.pipe()

        proc = MagicMock()
        proc.pid = 88888
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        # Write a partial JSON line — no newline; keep w_fd open to prevent EOF
        os.write(w_fd, b'{"type":"text","text":"partial-line-no-newline')

        start = time.time()
        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=0.3, total_deadline=5.0,
        ))
        elapsed = time.time() - start

        # Should fire at ~0.3s (chunk_timeout), NOT wait for total_deadline (5s)
        assert elapsed < 1.5, f"chunk_timeout not enforced: {elapsed:.2f}s (expected <1.5s)"
        assert events == []
        assert adapter.was_timed_out("T1")

        os.close(w_fd)

    def test_partial_line_then_complete_yields_event(self):
        """Partial data followed by the rest of the line + newline yields the event."""
        adapter = SubprocessAdapter()
        r_fd, w_fd = os.pipe()

        proc = MagicMock()
        proc.pid = 99990
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        payload = json.dumps({"type": "text", "text": "hello"})

        def writer():
            try:
                os.write(w_fd, payload[:12].encode())   # partial
                time.sleep(0.05)
                os.write(w_fd, (payload[12:] + "\n").encode())  # rest + newline
                time.sleep(0.05)
                os.close(w_fd)  # trigger EOF
            except OSError:
                pass

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=2.0, total_deadline=5.0,
        ))
        t.join(timeout=2)

        assert len(events) == 1
        assert events[0].type == "text"
        assert events[0].data.get("text") == "hello"

    def test_multiple_events_in_single_chunk(self):
        """Multiple complete JSON lines in one os.read() chunk are all yielded."""
        lines = [
            _json_line(type="init", session_id="s1"),
            _json_line(type="text", text="a"),
            _json_line(type="text", text="b"),
            _json_line(type="result", result="done"),
        ]
        adapter = SubprocessAdapter()
        proc = _make_pipe_proc(lines, returncode=0)
        adapter._processes["T1"] = proc

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=5.0, total_deadline=10.0,
        ))

        types = [e.type for e in events]
        assert "init" in types
        assert types.count("text") == 2
        assert "result" in types
        assert len(events) == 4

    def test_empty_lines_in_chunk_are_skipped(self):
        """Empty lines within a chunk (\\n\\n) do not produce events."""
        data = b"\n\n" + json.dumps({"type": "text", "text": "ok"}).encode() + b"\n\n"
        adapter = SubprocessAdapter()

        r_fd, w_fd = os.pipe()
        os.write(w_fd, data)
        os.close(w_fd)

        proc = MagicMock()
        proc.pid = 11110
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=5.0, total_deadline=10.0,
        ))

        assert len(events) == 1
        assert events[0].type == "text"

    def test_malformed_line_in_chunk_skipped_continues(self):
        """Malformed JSON within a chunk is skipped; subsequent valid lines still yield."""
        bad_line = b"NOT-JSON\n"
        good_line = json.dumps({"type": "text", "text": "good"}).encode() + b"\n"
        data = bad_line + good_line

        r_fd, w_fd = os.pipe()
        os.write(w_fd, data)
        os.close(w_fd)

        proc = MagicMock()
        proc.pid = 11120
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = os.fdopen(r_fd, "rb")

        adapter = SubprocessAdapter()
        adapter._processes["T1"] = proc

        events = list(adapter.read_events_with_timeout(
            "T1", chunk_timeout=5.0, total_deadline=10.0,
        ))

        assert len(events) == 1
        assert events[0].type == "text"

    def test_total_deadline_still_enforced_with_buffer(self):
        """total_deadline fires correctly even with the new buffered read path."""
        adapter = SubprocessAdapter()
        r_fd, w_fd = os.pipe()

        proc = MagicMock()
        proc.pid = 11130
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = os.fdopen(r_fd, "rb")
        adapter._processes["T1"] = proc

        # Drip events slowly to exceed total_deadline
        def drip():
            try:
                for i in range(100):
                    line = _json_line(type="text", text=f"m{i}")
                    os.write(w_fd, line)
                    time.sleep(0.15)
            except OSError:
                pass
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

        assert len(events) > 0
        assert len(events) < 100
        assert adapter.was_timed_out("T1")

        t.join(timeout=2)
