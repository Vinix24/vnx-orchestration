"""test_claude_spawn.py — Unit tests for provider_spawns/claude_spawn.py.

Covers:
1. completion_text accumulation from stream-json 'text' events.
2. completion_text fallback to result event 'text' when no text events emitted.
3. completion_text empty string when deliver() fails before event loop.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from provider_spawns.claude_spawn import ClaudeSpawnResult, spawn_claude


# ---------------------------------------------------------------------------
# Minimal StreamEvent stub (mirrors subprocess_adapter.StreamEvent shape)
# ---------------------------------------------------------------------------


@dataclass
class _FakeStreamEvent:
    type: str
    data: Dict[str, Any]
    timestamp: float = 0.0
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_event(text: str) -> _FakeStreamEvent:
    return _FakeStreamEvent(type="text", data={"text": text})


def _make_result_event(text: str = "") -> _FakeStreamEvent:
    return _FakeStreamEvent(
        type="result",
        data={
            "text": text,
            "subtype": "success",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


def _build_adapter_mock(events: List[_FakeStreamEvent], returncode: int = 0) -> MagicMock:
    """Build a SubprocessAdapter mock that yields the given events."""
    adapter = MagicMock()
    deliver_result = MagicMock()
    deliver_result.success = True
    adapter.deliver.return_value = deliver_result
    adapter.read_events_with_timeout.return_value = iter(events)
    adapter.was_timed_out.return_value = False
    obs = MagicMock()
    obs.transport_state = {"returncode": returncode}
    adapter.observe.return_value = obs
    adapter.get_session_id.return_value = "fake-session-id"
    adapter.event_store = None
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpawnClaudeAccumulatesCompletionText:
    """spawn_claude() must populate ClaudeSpawnResult.completion_text from text events."""

    def test_accumulates_assistant_text_from_stream_json(self):
        """Two text events → completion_text is their concatenation."""
        events = [
            _make_text_event("def add(a, b):"),
            _make_text_event("\n    return a + b"),
            _make_result_event("def add(a, b):\n    return a + b"),
        ]
        adapter_mock = _build_adapter_mock(events)

        with patch(
            "provider_spawns.claude_spawn.SubprocessAdapter",
            return_value=adapter_mock,
        ):
            result = spawn_claude(
                prompt="write add function",
                model="sonnet",
                dispatch_id="test-spawn-001",
                terminal_id="T1",
            )

        assert result.completion_text == "def add(a, b):\n    return a + b"

    def test_completion_text_fallback_to_result_event_when_no_text_events(self):
        """No text events → completion_text falls back to result event 'text'."""
        events = [
            _make_result_event("def foo(): pass"),
        ]
        adapter_mock = _build_adapter_mock(events)

        with patch(
            "provider_spawns.claude_spawn.SubprocessAdapter",
            return_value=adapter_mock,
        ):
            result = spawn_claude(
                prompt="write foo",
                model="haiku",
                dispatch_id="test-spawn-002",
                terminal_id="T1",
            )

        assert result.completion_text == "def foo(): pass"

    def test_completion_text_empty_when_deliver_fails(self):
        """When deliver() fails, completion_text is empty (no events processed)."""
        adapter = MagicMock()
        deliver_result = MagicMock()
        deliver_result.success = False
        deliver_result.failure_reason = "subprocess failed to start"
        adapter.deliver.return_value = deliver_result

        with patch(
            "provider_spawns.claude_spawn.SubprocessAdapter",
            return_value=adapter,
        ):
            result = spawn_claude(
                prompt="write something",
                model="sonnet",
                dispatch_id="test-spawn-003",
                terminal_id="T1",
            )

        assert result.completion_text == ""
        assert result.returncode == 1

    def test_completion_text_empty_when_no_events(self):
        """Empty event stream → completion_text is empty string."""
        adapter_mock = _build_adapter_mock([])

        with patch(
            "provider_spawns.claude_spawn.SubprocessAdapter",
            return_value=adapter_mock,
        ):
            result = spawn_claude(
                prompt="noop",
                model="haiku",
                dispatch_id="test-spawn-004",
                terminal_id="T1",
            )

        assert result.completion_text == ""
