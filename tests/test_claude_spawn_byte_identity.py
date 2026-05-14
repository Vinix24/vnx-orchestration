#!/usr/bin/env python3
"""test_claude_spawn_byte_identity.py — Wave 4.6 PR-4.6.2

Verifies structural identity of claude_spawn.spawn_claude():

  test_claude_spawn_matches_legacy_event_shape  — structural event shape parity
  test_claude_spawn_handles_completion          — ClaudeSpawnResult.completion dict
  test_claude_spawn_handles_premature_exit      — non-zero returncode + partial events
  test_claude_spawn_health_monitor_integration  — health_monitor.update() + cleanup

Skip tests that require a real claude CLI with NO_CLAUDE=1 env var or --no-claude
pytest marker (may be absent in CI environments without the CLI).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.claude_spawn import ClaudeSpawnResult, spawn_claude
from subprocess_adapter import StreamEvent

# ---------------------------------------------------------------------------
# Helpers: fixed StreamEvent sequences mirroring what SubprocessAdapter
# emits after normalising a real Claude stream-json sequence.
# ---------------------------------------------------------------------------

# Minimal normalised event sequence: init → text → result
_MINIMAL_EVENTS = [
    StreamEvent(type="init", data={"session_id": "sess-abc123", "model": "claude-sonnet-4-6"}, session_id="sess-abc123"),
    StreamEvent(type="text", data={"text": "OK"}),
    StreamEvent(type="result", data={"text": "OK", "subtype": "success", "session_id": "sess-abc123"}),
]

# Partial sequence — no result event (premature exit)
_PREMATURE_EVENTS = [
    StreamEvent(type="init", data={"session_id": "sess-early", "model": "sonnet"}, session_id="sess-early"),
    StreamEvent(type="text", data={"text": "partial"}),
]


# ---------------------------------------------------------------------------
# Fixture: mock SubprocessAdapter
# ---------------------------------------------------------------------------

def _make_adapter_mock(
    events: List[StreamEvent],
    returncode: int = 0,
    session_id: str = "sess-abc123",
) -> MagicMock:
    """Return a mock SubprocessAdapter pre-wired to yield ``events``."""
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
# Test 1: structural event shape matches legacy
# ---------------------------------------------------------------------------

class TestClaudeSpawnMatchesLegacyEventShape:
    """Structural identity: spawn_claude emits the same event types in the same
    order as the old inline SubprocessAdapter path."""

    def test_event_types_match_legacy_shape(self):
        """Spawn a minimal stream and verify emitted event types match legacy."""
        captured_events: List[Dict[str, Any]] = []

        def _capture_event(terminal_id, event_dict, dispatch_id=None):
            captured_events.append(event_dict)

        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                event_writer=_capture_event,
            )

        # Should have captured: init, text, result — in order
        event_types = [e["type"] for e in captured_events]
        assert "init" in event_types, f"init missing from {event_types}"
        assert "text" in event_types, f"text missing from {event_types}"
        assert "result" in event_types, f"result missing from {event_types}"

        idx_init = event_types.index("init")
        idx_text = event_types.index("text")
        idx_result = event_types.index("result")
        assert idx_init < idx_text < idx_result, (
            f"unexpected event order: {event_types}"
        )

    def test_session_id_extracted_from_init(self):
        """session_id returned in ClaudeSpawnResult comes from adapter.get_session_id()."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(
                _MINIMAL_EVENTS, session_id="sess-abc123"
            )

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
            )

        assert result.session_id == "sess-abc123"


# ---------------------------------------------------------------------------
# Test 2: completion dict captured from result event
# ---------------------------------------------------------------------------

class TestClaudeSpawnHandlesCompletion:
    """spawn_claude returns ClaudeSpawnResult with completion dict from result event."""

    def test_completion_dict_matches_result_event(self):
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
            )

        assert isinstance(result, ClaudeSpawnResult)
        assert result.returncode == 0
        assert result.completion.get("text") == "OK"
        assert result.completion.get("subtype") == "success"

    def test_events_written_count_matches_stream(self):
        """events_written counts all events yielded by read_events_with_timeout."""
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
            )

        # init=1, text=1, result=1 → 3 total
        assert result.events_written == 3, (
            f"expected 3 events_written, got {result.events_written}"
        )


# ---------------------------------------------------------------------------
# Test 3: premature exit (returncode=1, no result event)
# ---------------------------------------------------------------------------

class TestClaudeSpawnHandlesPrematureExit:
    """spawn_claude handles subprocess that exits before emitting a result event."""

    def test_returncode_1_on_premature_exit(self):
        captured: List[Dict] = []

        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(
                _PREMATURE_EVENTS, returncode=1, session_id="sess-early"
            )

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                event_writer=lambda tid, ev, dispatch_id=None: captured.append(ev),
            )

        assert result.returncode == 1, f"expected returncode=1, got {result.returncode}"
        # event_writer called for partial events (init + text)
        assert len(captured) >= 1, "event_writer not called for partial events"

    def test_completion_empty_on_premature_exit(self):
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(
                _PREMATURE_EVENTS, returncode=1, session_id="sess-early"
            )

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
            )

        assert result.completion == {}, (
            f"expected empty completion on premature exit, got {result.completion}"
        )


# ---------------------------------------------------------------------------
# Test 4: health_monitor integration
# ---------------------------------------------------------------------------

class TestClaudeSpawnHealthMonitorIntegration:
    """spawn_claude calls health_monitor.update() per event and wires event_store."""

    def test_health_monitor_update_called_per_event(self):
        health_monitor = MagicMock()
        health_monitor._event_store = None
        health_monitor.health_status.return_value = MagicMock(status="active")

        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                health_monitor=health_monitor,
            )

        # update() called once per event
        assert health_monitor.update.call_count == result.events_written, (
            f"health_monitor.update call count {health_monitor.update.call_count} "
            f"!= events_written {result.events_written}"
        )

    def test_health_monitor_event_store_wired(self):
        """When health_monitor._event_store is None and adapter has an event_store,
        spawn_claude wires them together before streaming begins."""
        fake_es = MagicMock()
        health_monitor = MagicMock()
        health_monitor._event_store = None

        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock(_MINIMAL_EVENTS)
            adapter_mock.event_store = fake_es
            MockAdapter.return_value = adapter_mock

            spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                health_monitor=health_monitor,
            )

        assert health_monitor._event_store is fake_es, (
            "spawn_claude should wire adapter.event_store into health_monitor._event_store"
        )


# ---------------------------------------------------------------------------
# Test 5: on_event callback for early stop (rotation)
# ---------------------------------------------------------------------------

class TestClaudeSpawnOnEventCallback:
    """on_event callback integration: return False to stop stream early."""

    def test_on_event_false_stops_stream(self):
        call_count = 0

        def _stop_after_first(event):
            nonlocal call_count
            call_count += 1
            return False

        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            adapter_mock = _make_adapter_mock(_MINIMAL_EVENTS)
            MockAdapter.return_value = adapter_mock

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                on_event=_stop_after_first,
            )

        assert call_count == 1
        assert result.stopped_early is True
        assert result.events_written == 1
        adapter_mock.stop.assert_called_once_with("T1")

    def test_on_event_true_continues_stream(self):
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            MockAdapter.return_value = _make_adapter_mock(_MINIMAL_EVENTS)

            result = spawn_claude(
                prompt="Reply OK.",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
                on_event=lambda e: True,
            )

        assert result.stopped_early is False
        assert result.events_written == 3


# ---------------------------------------------------------------------------
# Test 6: deliver() failure path
# ---------------------------------------------------------------------------

class TestClaudeSpawnDeliverFailure:
    """spawn_claude returns returncode=1 when SubprocessAdapter.deliver() fails."""

    def test_deliver_failure_returns_failed_result(self):
        with patch("provider_spawns.claude_spawn.SubprocessAdapter") as MockAdapter:
            instance = MagicMock()
            instance.deliver.return_value = MagicMock(
                success=False, failure_reason="claude not found"
            )
            instance.event_store = None
            MockAdapter.return_value = instance

            result = spawn_claude(
                prompt="test",
                model="sonnet",
                dispatch_id="test-dispatch",
                terminal_id="T1",
            )

        assert result.returncode == 1
        assert result.events_written == 0
        assert result.session_id is None
        assert result.timed_out is False
        assert result.completion == {}
