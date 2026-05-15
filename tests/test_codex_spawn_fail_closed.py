#!/usr/bin/env python3
"""test_codex_spawn_fail_closed.py — Wave 4.6 PR-4.6.3 fail-closed suite.

Verifies fail-closed behaviour in spawn_codex():

  test_missing_binary_raises_file_not_found — FileNotFoundError propagates
  test_broken_pipe_returns_failed_result    — BrokenPipeError → error result
  test_chunk_timeout_returns_timed_out      — chunk_timeout breach → timed_out=True
  test_on_event_false_stops_stream_early    — on_event=False → stopped_early=True
  test_normal_completion_unchanged          — happy path returncode==0 (regression)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.codex_spawn import CodexSpawnResult, _kill_proc, spawn_codex
from canonical_event import CanonicalEvent



# ---------------------------------------------------------------------------
# Test 1: missing binary raises FileNotFoundError
# ---------------------------------------------------------------------------

class TestCodexSpawnMissingBinary:
    """spawn_codex raises FileNotFoundError when codex binary is absent."""

    def test_missing_binary_raises_file_not_found(self):
        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            MockPopen.side_effect = FileNotFoundError("codex: not found")

            with pytest.raises(FileNotFoundError):
                spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id="test-missing-binary",
                    terminal_id="T1",
                )


# ---------------------------------------------------------------------------
# Test 2: BrokenPipeError on stdin write → error result
# ---------------------------------------------------------------------------

class TestCodexSpawnBrokenPipe:
    """spawn_codex returns CodexSpawnResult with error when stdin write fails."""

    def test_broken_pipe_returns_failed_result(self):
        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 1
            proc.wait = MagicMock(return_value=1)
            proc.poll = MagicMock(return_value=1)

            stdin_mock = MagicMock()
            stdin_mock.write.side_effect = BrokenPipeError("pipe broken")
            proc.stdin = stdin_mock

            MockPopen.return_value = proc

            result = spawn_codex(
                prompt="test",
                model="",
                dispatch_id="test-broken-pipe",
                terminal_id="T1",
            )

        assert isinstance(result, CodexSpawnResult)
        assert result.returncode == 1
        assert result.error is not None
        assert "BrokenPipeError" in result.error
        assert result.events_written == 0
        assert result.timed_out is False


# ---------------------------------------------------------------------------
# Test 3: chunk_timeout breach → timed_out=True
# ---------------------------------------------------------------------------

class TestCodexSpawnTimeout:
    """spawn_codex returns timed_out=True when drain_stream signals timeout."""

    def test_chunk_timeout_returns_timed_out(self):
        """When drain_stream emits a timeout error event, timed_out=True."""
        timeout_event = CanonicalEvent(
            dispatch_id="test-timeout",
            terminal_id="T1",
            provider="codex",
            event_type="error",
            data={"reason": "chunk timeout 60s exceeded"},
            observability_tier=1,
        )

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = -15
            proc.wait = MagicMock(return_value=-15)
            proc.poll = MagicMock(return_value=-15)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter([timeout_event]),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id="test-timeout",
                    terminal_id="T1",
                    chunk_timeout=1.0,
                    total_deadline=5.0,
                )

        assert result.timed_out is True, (
            f"expected timed_out=True after timeout error event, got {result.timed_out}"
        )


# ---------------------------------------------------------------------------
# Test 4: on_event=False stops stream early
# ---------------------------------------------------------------------------

class TestCodexSpawnOnEventStop:
    """spawn_codex sets stopped_early=True when on_event returns False."""

    def test_on_event_false_stops_stream_early(self):
        call_count = 0

        def _stop_after_first(event: CanonicalEvent):
            nonlocal call_count
            call_count += 1
            return False

        init_event = CanonicalEvent(
            dispatch_id="test-stop",
            terminal_id="T1",
            provider="codex",
            event_type="init",
            data={"raw_type": "thread.started"},
            observability_tier=1,
        )
        text_event = CanonicalEvent(
            dispatch_id="test-stop",
            terminal_id="T1",
            provider="codex",
            event_type="text",
            data={"text": "should not reach here"},
            observability_tier=1,
        )

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter([init_event, text_event]),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id="test-stop",
                    terminal_id="T1",
                    on_event=_stop_after_first,
                )

        assert result.stopped_early is True
        assert call_count == 1
        assert result.events_written == 1


# ---------------------------------------------------------------------------
# Test 5: happy-path regression
# ---------------------------------------------------------------------------

class TestCodexSpawnNormalCompletion:
    """Happy path: successful drain returns returncode==0 and populates result."""

    def test_normal_completion_unchanged(self):
        events = [
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="codex",
                event_type="init",
                data={"raw_type": "thread.started"},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="codex",
                event_type="text",
                data={"text": "Analysis complete."},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="codex",
                event_type="complete",
                data={},
                observability_tier=1,
            ),
        ]

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                result = spawn_codex(
                    prompt="Reply OK.",
                    model="",
                    dispatch_id="test-ok",
                    terminal_id="T1",
                )

        assert isinstance(result, CodexSpawnResult)
        assert result.returncode == 0
        assert result.events_written == 3
        assert result.timed_out is False
        assert result.stopped_early is False
        assert result.error is None
        assert "Analysis complete." in result.completion_text


# ---------------------------------------------------------------------------
# Test 6: event_writer failure is logged as ERROR and counted
# ---------------------------------------------------------------------------

class TestEventWriterFailureLogged:
    """ADR-005: event_writer failures logged at ERROR level and counted in result."""

    def _make_proc(self, MockPopen: MagicMock) -> MagicMock:
        proc = MagicMock()
        proc.pid = 99
        proc.returncode = 0
        proc.wait = MagicMock(return_value=0)
        proc.poll = MagicMock(return_value=0)
        proc.stdin = MagicMock()
        MockPopen.return_value = proc
        return proc

    def test_event_writer_failure_is_logged_as_error_and_counted(self, caplog):
        """When event_writer always raises, result.event_writer_failures > 0 and ERROR logged."""
        events = [
            CanonicalEvent(
                dispatch_id="test-ew-fail",
                terminal_id="T1",
                provider="codex",
                event_type="text",
                data={"text": "hello"},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ew-fail",
                terminal_id="T1",
                provider="codex",
                event_type="complete",
                data={},
                observability_tier=1,
            ),
        ]

        def _failing_writer(tid, event_dict, dispatch_id=None):
            raise OSError("ndjson ledger unavailable")

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            self._make_proc(MockPopen)

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                with caplog.at_level(logging.ERROR, logger="provider_spawns.codex_spawn"):
                    result = spawn_codex(
                        prompt="test",
                        model="",
                        dispatch_id="test-ew-fail",
                        terminal_id="T1",
                        event_writer=_failing_writer,
                    )

        assert result.event_writer_failures == 2, (
            f"expected 2 writer failures (one per event), got {result.event_writer_failures}"
        )
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) >= 1, "expected at least one ERROR log record"
        assert any(
            "event_writer callback failed" in r.message for r in error_records
        ), f"ERROR log missing 'event_writer callback failed': {[r.message for r in error_records]}"

    def test_event_writer_strict_raises_on_failure(self):
        """event_writer_strict=True raises RuntimeError when event_writer fails."""
        events = [
            CanonicalEvent(
                dispatch_id="test-strict",
                terminal_id="T1",
                provider="codex",
                event_type="text",
                data={"text": "hi"},
                observability_tier=1,
            ),
        ]

        def _failing_writer(tid, event_dict, dispatch_id=None):
            raise ValueError("write failed")

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            proc.stdin = MagicMock()
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                with pytest.raises(RuntimeError, match="event_writer failed"):
                    spawn_codex(
                        prompt="test",
                        model="",
                        dispatch_id="test-strict",
                        terminal_id="T1",
                        event_writer=_failing_writer,
                        event_writer_strict=True,
                    )

    def test_no_failures_result_field_zero(self):
        """event_writer_failures=0 when writer never raises (regression guard)."""
        events = [
            CanonicalEvent(
                dispatch_id="test-ok-ew",
                terminal_id="T1",
                provider="codex",
                event_type="text",
                data={"text": "ok"},
                observability_tier=1,
            ),
        ]

        collected: List[dict] = []

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            proc.stdin = MagicMock()
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id="test-ok-ew",
                    terminal_id="T1",
                    event_writer=lambda tid, ev, dispatch_id=None: collected.append(ev),
                )

        assert result.event_writer_failures == 0
        assert len(collected) == 1


# ---------------------------------------------------------------------------
# Test 7 (R2): _kill_proc fallback wait failure is logged, not silently swallowed
# ---------------------------------------------------------------------------

class TestKillProcFallbackWaitFailure:
    """R2 fix: fallback wait exceptions in _kill_proc must be logged as WARNING."""

    def _make_proc(self, pid: int = 42) -> MagicMock:
        proc = MagicMock(spec=subprocess.Popen)
        type(proc).pid = MagicMock(return_value=pid)
        proc.pid = pid
        return proc

    def test_kill_proc_fallback_wait_failure_is_logged(self, caplog):
        """When proc.wait(timeout=2) raises ProcessLookupError, WARNING is emitted."""
        proc = self._make_proc(pid=1234)
        proc.wait.side_effect = ProcessLookupError("process already gone")

        with patch("os.getpgid", side_effect=OSError("no pgid")), \
             caplog.at_level(logging.WARNING, logger="provider_spawns.codex_spawn"):
            _kill_proc(proc)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fallback wait failed" in m for m in warning_msgs), (
            f"Expected WARNING containing 'fallback wait failed', got: {warning_msgs}"
        )

    def test_kill_proc_fallback_wait_failure_includes_pid(self, caplog):
        """WARNING log must include the pid for forensics."""
        proc = self._make_proc(pid=5678)
        proc.wait.side_effect = ProcessLookupError("gone")

        with patch("os.getpgid", side_effect=OSError("no pgid")), \
             caplog.at_level(logging.WARNING, logger="provider_spawns.codex_spawn"):
            _kill_proc(proc)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("5678" in m for m in warning_msgs), (
            f"pid 5678 not in WARNING messages: {warning_msgs}"
        )

    def test_kill_proc_timeout_expired_is_logged(self, caplog):
        """When proc.wait(timeout=2) raises TimeoutExpired, WARNING is emitted."""
        proc = self._make_proc(pid=99)
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=2)

        with patch("os.getpgid", side_effect=OSError("no pgid")), \
             caplog.at_level(logging.WARNING, logger="provider_spawns.codex_spawn"):
            _kill_proc(proc)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fallback wait failed" in m for m in warning_msgs), (
            f"Expected WARNING containing 'fallback wait failed', got: {warning_msgs}"
        )

    def test_kill_proc_returns_normally_on_process_lookup_error(self):
        """_kill_proc must not propagate ProcessLookupError from proc.wait()."""
        proc = self._make_proc(pid=11)
        proc.wait.side_effect = ProcessLookupError("already gone")

        with patch("os.getpgid", side_effect=OSError("no pgid")):
            try:
                _kill_proc(proc)
            except Exception as exc:
                pytest.fail(f"_kill_proc propagated unexpectedly: {exc!r}")

    def test_kill_proc_returns_normally_on_timeout_expired(self):
        """_kill_proc must not propagate TimeoutExpired from proc.wait()."""
        proc = self._make_proc(pid=22)
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=2)

        with patch("os.getpgid", side_effect=OSError("no pgid")):
            try:
                _kill_proc(proc)
            except Exception as exc:
                pytest.fail(f"_kill_proc propagated unexpectedly: {exc!r}")
