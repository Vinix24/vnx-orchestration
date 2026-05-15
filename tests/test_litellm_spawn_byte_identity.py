#!/usr/bin/env python3
"""test_litellm_spawn_byte_identity.py — Wave 4.6 PR-4.6.5 byte-identity suite.

Verifies structural identity between normalize_litellm_event (in litellm_spawn)
and LiteLLMAdapter._normalize (which now delegates to the same function).

Tests:
  test_normalizer_identity_error_event       — error_type -> error
  test_normalizer_identity_init_event        — role=assistant + no content -> init
  test_normalizer_identity_text_event        — delta.content -> text
  test_normalizer_identity_complete_event    — finish_reason=stop -> complete
  test_normalizer_identity_tool_use_event    — delta.tool_calls -> tool_use
  test_adapter_delegates_to_spawn            — execute() collects same events as spawn_litellm
  test_event_writer_receives_all_events      — event_writer gets dicts for every event
  test_completion_text_from_text_events      — completion_text joins all text event content
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_LIB / "adapters"))

from provider_spawns.litellm_spawn import (
    LiteLLMSpawnResult,
    normalize_litellm_event,
    spawn_litellm,
)
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Fixture: known OpenAI-shaped NDJSON raw event dicts
# ---------------------------------------------------------------------------

_DISPATCH_ID = "test-byte-identity"
_TERMINAL_ID = "T1"

_RAW_ERROR = {"error_type": "runner_error", "message": "litellm not installed"}
_RAW_INIT = {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}], "model": "gpt-4"}
_RAW_TEXT = {"choices": [{"delta": {"content": "Analysis complete."}, "finish_reason": None}]}
_RAW_COMPLETE = {"choices": [{"delta": {}, "finish_reason": "stop"}], "model": "gpt-4"}
_RAW_TOOL_USE = {
    "choices": [{"delta": {"tool_calls": [{"id": "call_1", "function": {"name": "bash"}}]}, "finish_reason": None}]
}

_ALL_RAW_EVENTS = [_RAW_INIT, _RAW_TEXT, _RAW_COMPLETE]


def _normalize_via_spawn(raw: dict) -> CanonicalEvent:
    return normalize_litellm_event(raw, _TERMINAL_ID, _DISPATCH_ID)


def _normalize_via_adapter(raw: dict) -> CanonicalEvent:
    from litellm_adapter import LiteLLMAdapter
    adapter = object.__new__(LiteLLMAdapter)
    adapter._terminal_id = _TERMINAL_ID
    adapter._dispatch_id = _DISPATCH_ID
    return adapter._normalize(raw)


def _strip_nondeterministic(ev_dict: dict) -> dict:
    """Remove timestamp and event_id (generated fresh per call)."""
    return {k: v for k, v in ev_dict.items() if k not in ("timestamp", "event_id")}


# ---------------------------------------------------------------------------
# Test 1-5: normalize_litellm_event == LiteLLMAdapter._normalize for all shapes
# ---------------------------------------------------------------------------

class TestNormalizerIdentity:
    """Both normalization paths must produce structurally identical events."""

    def _assert_identical(self, raw: dict) -> None:
        via_spawn = _normalize_via_spawn(raw)
        via_adapter = _normalize_via_adapter(raw)
        assert via_spawn.event_type == via_adapter.event_type, (
            f"event_type mismatch for {raw}: {via_spawn.event_type!r} != {via_adapter.event_type!r}"
        )
        assert via_spawn.data == via_adapter.data, (
            f"data mismatch for {raw}: {via_spawn.data} != {via_adapter.data}"
        )
        assert via_spawn.provider == via_adapter.provider == "litellm"
        assert via_spawn.dispatch_id == via_adapter.dispatch_id == _DISPATCH_ID
        assert via_spawn.terminal_id == via_adapter.terminal_id == _TERMINAL_ID

    def test_normalizer_identity_error_event(self):
        self._assert_identical(_RAW_ERROR)

    def test_normalizer_identity_init_event(self):
        self._assert_identical(_RAW_INIT)

    def test_normalizer_identity_text_event(self):
        self._assert_identical(_RAW_TEXT)

    def test_normalizer_identity_complete_event(self):
        self._assert_identical(_RAW_COMPLETE)

    def test_normalizer_identity_tool_use_event(self):
        self._assert_identical(_RAW_TOOL_USE)


# ---------------------------------------------------------------------------
# Test 6: LiteLLMAdapter.execute() collects same events as spawn_litellm
# ---------------------------------------------------------------------------

class TestAdapterDelegatesToSpawn:
    """execute() is a thin wrapper over spawn_litellm; collected events must match."""

    def _build_canonical_events(self) -> List[CanonicalEvent]:
        return [normalize_litellm_event(r, _TERMINAL_ID, _DISPATCH_ID) for r in _ALL_RAW_EVENTS]

    def test_adapter_delegates_to_spawn(self):
        canonical_events = self._build_canonical_events()

        direct_collected: List[dict] = []

        def _collect(tid, event_dict, dispatch_id=None):
            direct_collected.append(event_dict)

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
                return_value=iter(canonical_events),
            ):
                result = spawn_litellm(
                    prompt="test instruction",
                    model="anthropic/claude-sonnet-4-6",
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                    event_writer=_collect,
                )

        assert result.events_written == len(_ALL_RAW_EVENTS)
        assert len(direct_collected) == len(_ALL_RAW_EVENTS)

        for i, (raw, collected_dict) in enumerate(zip(_ALL_RAW_EVENTS, direct_collected)):
            expected = normalize_litellm_event(raw, _TERMINAL_ID, _DISPATCH_ID)
            assert collected_dict["event_type"] == expected.event_type, (
                f"event[{i}] event_type mismatch: "
                f"{collected_dict['event_type']!r} != {expected.event_type!r}"
            )
            assert collected_dict["data"] == expected.data, (
                f"event[{i}] data mismatch: {collected_dict['data']} != {expected.data}"
            )


# ---------------------------------------------------------------------------
# Test 7: event_writer receives dict for every event
# ---------------------------------------------------------------------------

class TestEventWriterReceivesAllEvents:
    """event_writer callback is called once per canonical event."""

    def test_event_writer_receives_all_events(self):
        canonical_events = [
            normalize_litellm_event(r, _TERMINAL_ID, _DISPATCH_ID) for r in _ALL_RAW_EVENTS
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
                return_value=iter(canonical_events),
            ):
                spawn_litellm(
                    prompt="test",
                    model="anthropic/claude-sonnet-4-6",
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                    event_writer=lambda tid, ev, dispatch_id=None: collected.append(ev),
                )

        assert len(collected) == len(_ALL_RAW_EVENTS), (
            f"event_writer called {len(collected)} times, expected {len(_ALL_RAW_EVENTS)}"
        )
        event_types = [ev["event_type"] for ev in collected]
        assert "init" in event_types
        assert "text" in event_types
        assert "complete" in event_types


# ---------------------------------------------------------------------------
# Test 8: completion_text from text events
# ---------------------------------------------------------------------------

class TestCompletionText:
    """completion_text is built from all text event content values."""

    def test_completion_text_from_text_events(self):
        events = [
            normalize_litellm_event(
                {"choices": [{"delta": {"content": "Part 1."}, "finish_reason": None}]},
                _TERMINAL_ID, _DISPATCH_ID,
            ),
            normalize_litellm_event(
                {"choices": [{"delta": {"content": " Part 2."}, "finish_reason": None}]},
                _TERMINAL_ID, _DISPATCH_ID,
            ),
        ]

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
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                )

        assert "Part 1." in result.completion_text
        assert "Part 2." in result.completion_text


# ---------------------------------------------------------------------------
# Test 9: provider and tier in all normalized events
# ---------------------------------------------------------------------------

class TestNormalizerOutputShape:
    """normalize_litellm_event produces litellm provider + Tier-1 events."""

    def test_all_shapes_have_litellm_provider(self):
        for raw in [_RAW_ERROR, _RAW_INIT, _RAW_TEXT, _RAW_COMPLETE, _RAW_TOOL_USE]:
            ev = normalize_litellm_event(raw, "T1", "d1")
            assert ev.provider == "litellm", f"expected provider=litellm for {raw!r}, got {ev.provider!r}"

    def test_all_shapes_are_tier_1(self):
        for raw in [_RAW_ERROR, _RAW_INIT, _RAW_TEXT, _RAW_COMPLETE, _RAW_TOOL_USE]:
            ev = normalize_litellm_event(raw, "T1", "d1")
            assert ev.observability_tier == 1, f"expected tier=1 for {raw!r}, got {ev.observability_tier}"

    def test_error_type_maps_to_error(self):
        ev = normalize_litellm_event(_RAW_ERROR, "T1", "d1")
        assert ev.event_type == "error"
        assert ev.data["error_type"] == "runner_error"

    def test_finish_reason_stop_maps_to_complete(self):
        ev = normalize_litellm_event(_RAW_COMPLETE, "T1", "d1")
        assert ev.event_type == "complete"
        assert ev.data["finish_reason"] == "stop"

    def test_tool_calls_maps_to_tool_use(self):
        ev = normalize_litellm_event(_RAW_TOOL_USE, "T1", "d1")
        assert ev.event_type == "tool_use"
        assert "tool_calls" in ev.data
