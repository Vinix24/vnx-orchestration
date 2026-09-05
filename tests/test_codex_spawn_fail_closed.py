#!/usr/bin/env python3
"""test_codex_spawn_fail_closed.py — Wave 4.6 PR-4.6.3/R3 fail-closed suite.

Verifies fail-closed behaviour in spawn_codex():

  test_spawn_returns_structured_result_when_binary_missing — missing binary → structured result (returncode=127)
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

from provider_spawns.codex_spawn import CodexSpawnResult, _build_cmd, _kill_proc, spawn_codex
from canonical_event import CanonicalEvent


def _mock_stderr(*lines: bytes) -> MagicMock:
    """Build a stderr mock whose readline terminates on b"" (OI-910).

    A real codex subprocess opens stderr in binary mode, so readline() returns
    bytes and yields b"" at EOF. Without a terminating readline, the drain
    thread in _launch_codex_proc spins forever on the b"" sentinel and leaks a
    daemon thread into the rest of the test session, which pytest-timeout then
    kills mid-suite (INTERNALERROR).
    """
    stderr = MagicMock()
    stderr.readline.side_effect = [*lines, b""]
    return stderr


# ---------------------------------------------------------------------------
# Test 0: model defaults and explicit override
# ---------------------------------------------------------------------------

class TestCodexSpawnModelResolution:
    """codex_spawn uses the current Codex CLI default unless explicitly overridden."""

    def test_build_cmd_defaults_to_gpt55(self, monkeypatch):
        monkeypatch.delenv("VNX_CODEX_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("VNX_CODEX_SANDBOX", raising=False)
        assert _build_cmd("") == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-5.5",
        ]

    def test_build_cmd_uses_env_default(self, monkeypatch):
        monkeypatch.setenv("VNX_CODEX_DEFAULT_MODEL", "gpt-5.5-test")
        monkeypatch.delenv("VNX_CODEX_SANDBOX", raising=False)
        assert _build_cmd("") == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-5.5-test",
        ]

    def test_build_cmd_explicit_model_overrides_env_default(self, monkeypatch):
        monkeypatch.setenv("VNX_CODEX_DEFAULT_MODEL", "gpt-5.5-test")
        monkeypatch.delenv("VNX_CODEX_SANDBOX", raising=False)
        assert _build_cmd("gpt-5.5") == [
            "codex", "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-5.5",
        ]



# ---------------------------------------------------------------------------
# Test 1: missing binary returns structured CodexSpawnResult (R3 fix)
# ---------------------------------------------------------------------------

class TestCodexSpawnMissingBinary:
    """spawn_codex returns structured result (returncode=127) when codex binary is absent."""

    def test_spawn_returns_structured_result_when_binary_missing(self):
        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            MockPopen.side_effect = FileNotFoundError("codex: not found")

            result = spawn_codex(
                prompt="test",
                model="",
                dispatch_id="test-missing-binary",
                terminal_id="T1",
            )

        assert isinstance(result, CodexSpawnResult), (
            f"Expected CodexSpawnResult, got {type(result)}"
        )
        assert result.returncode == 127, (
            f"Expected returncode=127 for missing binary, got {result.returncode}"
        )
        assert result.error is not None, "Expected error field to be set"
        assert "not found" in (result.error or "").lower(), (
            f"Expected 'not found' in error message, got: {result.error!r}"
        )
        assert result.events_written == 0
        assert result.timed_out is False
        assert result.completion_text == ""


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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")

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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")
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
# OI-1628: a task_complete-derived "error" canonical event's message must
# reach CodexSpawnResult.error, not vanish (previously only "reason" was
# read, to flip timed_out — a real error message was captured by no one and
# the caller fell back to the generic "codex stderr tail" surfacing).
# ---------------------------------------------------------------------------

class TestCodexSpawnQuotaExhaustion:
    """spawn_codex surfaces a codex quota/usage-limit error into result.error."""

    def test_task_complete_usage_limit_error_reaches_result_error(self):
        error_event = CanonicalEvent(
            dispatch_id="test-quota",
            terminal_id="T1",
            provider="codex",
            event_type="error",
            data={
                "message": (
                    "You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit "
                    "https://chatgpt.com/codex/settings/usage to purchase "
                    "more credits or try again at 2:27 PM. "
                    "(codex_error_info: usage_limit_exceeded)"
                )
            },
            observability_tier=1,
        )

        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 1
            proc.wait = MagicMock(return_value=1)
            proc.poll = MagicMock(return_value=1)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            proc.stderr = _mock_stderr(b"Reading prompt from stdin...\n")
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter([error_event]),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id="test-quota",
                    terminal_id="T1",
                )

        assert result.error is not None, "Expected result.error to be populated"
        assert "usage_limit_exceeded" in result.error, (
            f"Expected the captured quota message in result.error, got: {result.error!r}"
        )


# ---------------------------------------------------------------------------
# OI-1633: the first error to arrive on the stream must not automatically win
# once a transient (mid-stream) error and a terminal (task_complete) error
# both occur in the same run. Rank by terminality: a terminal error is
# stronger evidence and must win over an earlier OR later transient one; a
# terminal error must not be displaced by a later terminal either.
# ---------------------------------------------------------------------------

class TestCodexSpawnErrorRanking:
    """spawn_codex ranks a task_complete (terminal) error above a mid-stream (transient) one."""

    def _transient_reconnect_event(self, dispatch_id: str) -> CanonicalEvent:
        """A codex top-level `error` event the process itself recovers from.

        Real shape observed 2026-09-05 (15:33 run): codex emits this as a
        top-level `error` NDJSON event, then keeps streaming and eventually
        reaches task_complete. normalize_codex_event() marks this
        non-terminal (terminal=False) for exactly this reason.
        """
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            provider="codex",
            event_type="error",
            data={
                "message": (
                    "Reconnecting... 2/2 (stream disconnected before "
                    "completion: idle timeout)"
                ),
                "terminal": False,
            },
            observability_tier=1,
        )

    def _terminal_quota_event(self, dispatch_id: str) -> CanonicalEvent:
        """A task_complete-wrapped error — the real, terminal outcome of the turn."""
        return CanonicalEvent(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            provider="codex",
            event_type="error",
            data={
                "message": (
                    "You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit "
                    "https://chatgpt.com/codex/settings/usage to purchase "
                    "more credits or try again at 2:27 PM. "
                    "(codex_error_info: usage_limit_exceeded)"
                ),
                "terminal": True,
            },
            observability_tier=1,
        )

    def _run_with_events(self, dispatch_id: str, events: List[CanonicalEvent]):
        with patch("provider_spawns.codex_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 1
            proc.wait = MagicMock(return_value=1)
            proc.poll = MagicMock(return_value=1)
            proc.stdin = MagicMock()
            proc.stderr = _mock_stderr(b"Reading prompt from stdin...\n")
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                return spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id=dispatch_id,
                    terminal_id="T1",
                )

    def test_transient_then_terminal_keeps_terminal_reason(self):
        """Run-1 order (transient arrives first): the terminal reason must win.

        Pre-fix this was RED: first-wins captured the "Reconnecting..." text
        and the terminal quota message was discarded.
        """
        events = [
            self._transient_reconnect_event("test-rank-1"),
            self._terminal_quota_event("test-rank-1"),
        ]
        result = self._run_with_events("test-rank-1", events)

        assert result.error is not None
        assert "usage_limit_exceeded" in result.error, (
            f"expected the terminal quota reason to win, got: {result.error!r}"
        )
        assert "Reconnecting" not in result.error, (
            f"transient reconnect text must not survive in the final reason, got: {result.error!r}"
        )

    def test_terminal_then_transient_keeps_terminal_reason(self):
        """Reverse order (terminal arrives first): a later transient must not displace it.

        Guards against overcorrecting into last-wins, which would fail this case.
        """
        events = [
            self._terminal_quota_event("test-rank-2"),
            self._transient_reconnect_event("test-rank-2"),
        ]
        result = self._run_with_events("test-rank-2", events)

        assert result.error is not None
        assert "usage_limit_exceeded" in result.error, (
            f"expected the terminal quota reason to win, got: {result.error!r}"
        )
        assert "Reconnecting" not in result.error, (
            f"a later transient must not displace the captured terminal reason, got: {result.error!r}"
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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")
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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")
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
        proc.stderr = _mock_stderr(b"warn: codex sandbox")
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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")
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
            proc.stderr = _mock_stderr(b"warn: codex sandbox")
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


# ---------------------------------------------------------------------------
# Test 8 (OI-910): a non-terminating stderr stream must not leak the drain thread
# ---------------------------------------------------------------------------

class TestCodexSpawnDrainThreadLeak:
    """OI-910: a stderr stream that never returns b"" must not leak the drain thread.

    Pre-fix, a mocked Popen stderr makes readline() return a fresh non-empty
    Mock forever, so the b"" sentinel loop in _launch_codex_proc never ends. The
    daemon thread keeps consuming the stream, and pytest-timeout eventually kills
    it mid-suite — crashing the whole pytest session with INTERNALERROR at ~16%.
    """

    def test_drain_stderr_thread_terminates_on_nonterminating_stream(self, monkeypatch):
        import time

        import provider_spawns.codex_spawn as cs

        # Tight cap so the guard trips quickly. On the pre-fix code this constant
        # does not exist, the thread spins unbounded, and the tail buffer keeps
        # growing — which is exactly the leak this test catches (red on old code).
        if hasattr(cs, "_STDERR_DRAIN_MAX_LINES"):
            monkeypatch.setattr(cs, "_STDERR_DRAIN_MAX_LINES", 5)

        proc = MagicMock()
        proc.pid = 99
        proc.returncode = 0
        proc.wait = MagicMock(return_value=0)
        proc.poll = MagicMock(return_value=0)
        proc.stdin = MagicMock()
        # readline never returns b"" — a stream that never reaches EOF.
        proc.stderr = MagicMock()
        proc.stderr.readline.return_value = b"x"

        with patch("provider_spawns.codex_spawn.subprocess.Popen", return_value=proc), \
             patch(
                 "provider_spawns.codex_spawn._NormalizerHost.drain_stream",
                 return_value=iter([]),
             ):
            result = spawn_codex(
                prompt="test",
                model="",
                dispatch_id="test-drain-leak",
                terminal_id="T1",
            )

        assert result.returncode == 0

        # Give the drain thread a chance to consume the stream: on fixed code it
        # exits after the cap; on broken code it keeps appending forever.
        time.sleep(0.2)
        tail = list(proc._vnx_stderr_tail)
        time.sleep(0.2)
        assert list(proc._vnx_stderr_tail) == tail, (
            "drain thread still alive: the stderr stream never reached EOF and "
            "the daemon thread kept consuming it (OI-910 leak)"
        )
