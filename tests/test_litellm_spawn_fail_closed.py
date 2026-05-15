#!/usr/bin/env python3
"""test_litellm_spawn_fail_closed.py — Wave 4.6 PR-4.6.5 fail-closed suite.

Verifies fail-closed behaviour in spawn_litellm():

  test_spawn_returns_structured_result_when_binary_missing — FileNotFoundError → returncode=127
  test_broken_pipe_returns_failed_result    — BrokenPipeError → error result
  test_chunk_timeout_returns_timed_out      — chunk_timeout breach → timed_out=True
  test_on_event_false_stops_stream_early    — on_event=False → stopped_early=True
  test_normal_completion_unchanged          — happy path returncode==0 (regression)
  test_event_writer_failure_logged_and_counted — ADR-005: ERROR log + counter
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.litellm_spawn import LiteLLMSpawnResult, spawn_litellm
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Test 1: FileNotFoundError on Popen → structured LiteLLMSpawnResult(returncode=127)
# ---------------------------------------------------------------------------

class TestLiteLLMSpawnMissingBinary:
    """spawn_litellm returns structured result (returncode=127) when subprocess.Popen raises FileNotFoundError."""

    def test_spawn_returns_structured_result_when_binary_missing(self):
        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            MockPopen.side_effect = FileNotFoundError("python3: not found")

            result = spawn_litellm(
                prompt="test",
                model="anthropic/claude-sonnet-4-6",
                dispatch_id="test-missing-binary",
                terminal_id="T1",
            )

        assert isinstance(result, LiteLLMSpawnResult), (
            f"Expected LiteLLMSpawnResult, got {type(result)}"
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

class TestLiteLLMSpawnBrokenPipe:
    """spawn_litellm returns LiteLLMSpawnResult with error when stdin write fails."""

    def test_broken_pipe_returns_failed_result(self):
        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 1
            proc.wait = MagicMock(return_value=1)
            proc.poll = MagicMock(return_value=1)

            stdin_mock = MagicMock()
            stdin_mock.write.side_effect = BrokenPipeError("pipe broken")
            proc.stdin = stdin_mock

            MockPopen.return_value = proc

            result = spawn_litellm(
                prompt="test",
                model="anthropic/claude-sonnet-4-6",
                dispatch_id="test-broken-pipe",
                terminal_id="T1",
            )

        assert isinstance(result, LiteLLMSpawnResult)
        assert result.returncode == 1
        assert result.error is not None
        assert "BrokenPipeError" in result.error
        assert result.events_written == 0
        assert result.timed_out is False


# ---------------------------------------------------------------------------
# Test 3: chunk_timeout breach → timed_out=True
# ---------------------------------------------------------------------------

class TestLiteLLMSpawnTimeout:
    """spawn_litellm returns timed_out=True when drain_stream signals timeout."""

    def test_chunk_timeout_returns_timed_out(self):
        timeout_event = CanonicalEvent(
            dispatch_id="test-timeout",
            terminal_id="T1",
            provider="litellm",
            event_type="error",
            data={"reason": "chunk timeout 60s exceeded"},
            observability_tier=1,
        )

        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = -15
            proc.wait = MagicMock(return_value=-15)
            proc.poll = MagicMock(return_value=-15)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                return_value=iter([timeout_event]),
            ):
                result = spawn_litellm(
                    prompt="test",
                    model="anthropic/claude-sonnet-4-6",
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

class TestLiteLLMSpawnOnEventStop:
    """spawn_litellm sets stopped_early=True when on_event returns False."""

    def test_on_event_false_stops_stream_early(self):
        call_count = 0

        def _stop_after_first(event: CanonicalEvent):
            nonlocal call_count
            call_count += 1
            return False

        init_event = CanonicalEvent(
            dispatch_id="test-stop",
            terminal_id="T1",
            provider="litellm",
            event_type="init",
            data={"model": "anthropic/claude-sonnet-4-6"},
            observability_tier=1,
        )
        text_event = CanonicalEvent(
            dispatch_id="test-stop",
            terminal_id="T1",
            provider="litellm",
            event_type="text",
            data={"content": "should not reach here"},
            observability_tier=1,
        )

        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                return_value=iter([init_event, text_event]),
            ):
                result = spawn_litellm(
                    prompt="test",
                    model="anthropic/claude-sonnet-4-6",
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

class TestLiteLLMSpawnNormalCompletion:
    """Happy path: successful drain returns returncode==0 and populates result."""

    def test_normal_completion_unchanged(self):
        events = [
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="litellm",
                event_type="init",
                data={"model": "anthropic/claude-sonnet-4-6"},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="litellm",
                event_type="text",
                data={"content": "Analysis complete."},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ok",
                terminal_id="T1",
                provider="litellm",
                event_type="complete",
                data={"finish_reason": "stop", "model": "anthropic/claude-sonnet-4-6"},
                observability_tier=1,
            ),
        ]

        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            stdin_mock = MagicMock()
            proc.stdin = stdin_mock
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                result = spawn_litellm(
                    prompt="Reply OK.",
                    model="anthropic/claude-sonnet-4-6",
                    dispatch_id="test-ok",
                    terminal_id="T1",
                )

        assert isinstance(result, LiteLLMSpawnResult)
        assert result.returncode == 0
        assert result.events_written == 3
        assert result.timed_out is False
        assert result.stopped_early is False
        assert result.error is None
        assert "Analysis complete." in result.completion_text


# ---------------------------------------------------------------------------
# Test 6: event_writer failure is logged as ERROR and counted (ADR-005)
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
        events = [
            CanonicalEvent(
                dispatch_id="test-ew-fail",
                terminal_id="T1",
                provider="litellm",
                event_type="text",
                data={"content": "hello"},
                observability_tier=1,
            ),
            CanonicalEvent(
                dispatch_id="test-ew-fail",
                terminal_id="T1",
                provider="litellm",
                event_type="complete",
                data={"finish_reason": "stop", "model": ""},
                observability_tier=1,
            ),
        ]

        def _failing_writer(tid, event_dict, dispatch_id=None):
            raise OSError("ndjson ledger unavailable")

        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            self._make_proc(MockPopen)

            with patch(
                "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                with caplog.at_level(logging.ERROR, logger="provider_spawns.litellm_spawn"):
                    result = spawn_litellm(
                        prompt="test",
                        model="anthropic/claude-sonnet-4-6",
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

    def test_no_failures_result_field_zero(self):
        """event_writer_failures=0 when writer never raises (regression guard)."""
        events = [
            CanonicalEvent(
                dispatch_id="test-ok-ew",
                terminal_id="T1",
                provider="litellm",
                event_type="text",
                data={"content": "ok"},
                observability_tier=1,
            ),
        ]

        collected: List[dict] = []

        with patch("provider_spawns.litellm_spawn.subprocess.Popen") as MockPopen:
            proc = MagicMock()
            proc.pid = 99
            proc.returncode = 0
            proc.wait = MagicMock(return_value=0)
            proc.poll = MagicMock(return_value=0)
            proc.stdin = MagicMock()
            MockPopen.return_value = proc

            with patch(
                "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                return_value=iter(events),
            ):
                result = spawn_litellm(
                    prompt="test",
                    model="anthropic/claude-sonnet-4-6",
                    dispatch_id="test-ok-ew",
                    terminal_id="T1",
                    event_writer=lambda tid, ev, dispatch_id=None: collected.append(ev),
                )

        assert result.event_writer_failures == 0
        assert len(collected) == 1
