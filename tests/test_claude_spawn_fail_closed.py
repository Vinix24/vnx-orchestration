#!/usr/bin/env python3
"""test_claude_spawn_fail_closed.py — codex round-1 regression suite.

Verifies fail-closed stream-read exception handling in spawn_claude():

  test_stream_read_exception_returns_nonzero   — OSError mid-stream → returncode != 0
  test_stream_read_exception_kills_subprocess  — adapter.stop() is called on exception
  test_stream_read_exception_logs_error        — logger.error is called with exc message
  test_normal_completion_unchanged             — happy path returncode == 0 (regression)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.claude_spawn import ClaudeSpawnResult, spawn_claude
from subprocess_adapter import StreamEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_EVENTS = [
    StreamEvent(type="init", data={"session_id": "sess-ok", "model": "sonnet"}, session_id="sess-ok"),
    StreamEvent(type="text", data={"text": "OK"}),
    StreamEvent(type="result", data={"text": "OK", "subtype": "success", "session_id": "sess-ok"}),
]


def _make_adapter_mock(
    events: List[StreamEvent],
    returncode: int | None = 0,
    session_id: str = "sess-ok",
) -> MagicMock:
    instance = MagicMock()
    instance.deliver.return_value = MagicMock(success=True, failure_reason=None)
    instance.read_events_with_timeout.return_value = iter(events)
    instance.get_session_id.return_value = session_id
    instance.was_timed_out.return_value = False
    instance.observe.return_value = MagicMock(transport_state={"returncode": returncode})
    instance.event_store = None
    instance._returncode_cache = {}
    instance.stop = MagicMock()
    return instance


# ---------------------------------------------------------------------------
# Fail-closed tests
# ---------------------------------------------------------------------------

class TestClaudeSpawnFailClosed:
    """spawn_claude returns non-zero and stops subprocess when stream read raises."""

    def test_stream_read_exception_returns_nonzero(self):
        """OSError mid-stream must not produce returncode=0."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock([], returncode=None)
            adapter_mock.read_events_with_timeout.side_effect = OSError("connection lost")
            MockAdapter.return_value = adapter_mock

            result = spawn_claude(
                prompt="test",
                model="sonnet",
                dispatch_id="test-fail-closed",
                terminal_id="T1",
            )

        assert result.returncode != 0, (
            f"expected non-zero returncode on stream OSError, got {result.returncode}"
        )

    def test_stream_read_exception_returncode_when_proc_returns_zero(self):
        """Even if observe() returns 0 after stop, returncode must be non-zero."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock([], returncode=0)
            adapter_mock.read_events_with_timeout.side_effect = OSError("pipe broken")
            MockAdapter.return_value = adapter_mock

            result = spawn_claude(
                prompt="test",
                model="sonnet",
                dispatch_id="test-fail-closed-zero-rc",
                terminal_id="T1",
            )

        assert result.returncode != 0, (
            f"observe() returned 0 but stream exception must still produce non-zero, got {result.returncode}"
        )

    def test_stream_read_exception_kills_subprocess(self):
        """adapter.stop(terminal_id) must be called when stream read raises."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock([], returncode=None)
            adapter_mock.read_events_with_timeout.side_effect = OSError("connection lost")
            MockAdapter.return_value = adapter_mock

            spawn_claude(
                prompt="test",
                model="sonnet",
                dispatch_id="test-fail-closed",
                terminal_id="T1",
            )

        adapter_mock.stop.assert_called_with("T1")

    def test_stream_read_exception_logs_error(self):
        """logger.error must be called and include the exception message."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock([], returncode=None)
            adapter_mock.read_events_with_timeout.side_effect = OSError("connection lost")
            MockAdapter.return_value = adapter_mock

            with patch("provider_spawns.claude_spawn.logger") as mock_logger:
                spawn_claude(
                    prompt="test",
                    model="sonnet",
                    dispatch_id="test-fail-closed",
                    terminal_id="T1",
                )

            mock_logger.error.assert_called_once()
            call_args = str(mock_logger.error.call_args)
            assert "connection lost" in call_args, (
                f"logger.error call args do not contain exception message: {call_args}"
            )

    def test_stream_read_exception_error_field_set(self):
        """ClaudeSpawnResult.error must be set to the exception string."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock([], returncode=None)
            adapter_mock.read_events_with_timeout.side_effect = OSError("pipe closed")
            MockAdapter.return_value = adapter_mock

            result = spawn_claude(
                prompt="test",
                model="sonnet",
                dispatch_id="test-fail-closed",
                terminal_id="T1",
            )

        assert result.error is not None
        assert "pipe closed" in result.error

    def test_normal_completion_unchanged(self):
        """Happy path: successful stream still returns returncode=0 (regression)."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS, returncode=0)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-regression-ok",
                terminal_id="T1",
            )

        assert result.returncode == 0, (
            f"happy path must still return 0, got {result.returncode}"
        )
        assert result.completion.get("subtype") == "success"
        assert result.events_written == 3
        assert result.error is None
