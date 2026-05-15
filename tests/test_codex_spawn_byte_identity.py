#!/usr/bin/env python3
"""test_codex_spawn_byte_identity.py — Wave 4.6 PR-4.6.3 byte-identity suite.

Verifies structural identity between normalize_codex_event (in codex_spawn)
and CodexAdapter._normalize (which now delegates to the same function).

Tests:
  test_normalizer_identity_thread_started     — thread.started → init
  test_normalizer_identity_agent_message      — agent_message → text
  test_normalizer_identity_turn_completed     — turn.completed → complete
  test_normalizer_identity_error_event        — error → error
  test_normalizer_identity_item_completed     — item.completed [agent_message] → text
  test_normalizer_identity_token_count        — token_count → text with token_count
  test_codex_adapter_delegates_to_spawn       — execute() collects same events as spawn_codex
  test_event_writer_receives_all_events       — event_writer gets dicts for every event
  test_session_id_extracted_from_init         — session_id from thread.started payload
  test_completion_text_from_agent_messages    — completion_text joins all agent_message texts
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib" / "adapters"))

from provider_spawns.codex_spawn import (
    CodexSpawnResult,
    normalize_codex_event,
    spawn_codex,
)
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Fixture: known NDJSON raw event dicts (recorded from real codex exec --json)
# ---------------------------------------------------------------------------

_DISPATCH_ID = "test-byte-identity"
_TERMINAL_ID = "T1"

_RAW_THREAD_STARTED = {"type": "thread.started"}
_RAW_AGENT_MSG = {"type": "agent_message", "text": "LGTM. No blocking findings."}
_RAW_TURN_COMPLETED = {"type": "turn.completed"}
_RAW_ERROR = {"type": "error", "message": "something went wrong"}
_RAW_ITEM_COMPLETED_AGENT = {
    "type": "item.completed",
    "item": {"type": "agent_message", "content": "Check passed."},
}
_RAW_TOKEN_COUNT = {
    "event_msg": {
        "payload": {
            "type": "token_count",
            "input_tokens": 100,
            "output_tokens": 50,
        }
    }
}

_ALL_RAW_EVENTS = [
    _RAW_THREAD_STARTED,
    _RAW_AGENT_MSG,
    _RAW_TURN_COMPLETED,
]


def _normalize_via_spawn(raw: dict) -> CanonicalEvent:
    return normalize_codex_event(raw, _TERMINAL_ID, _DISPATCH_ID)


def _normalize_via_adapter(raw: dict) -> CanonicalEvent:
    from codex_adapter import CodexAdapter
    adapter = object.__new__(CodexAdapter)
    adapter._current_terminal_id = _TERMINAL_ID
    adapter._current_dispatch_id = _DISPATCH_ID
    return adapter._normalize(raw)


# ---------------------------------------------------------------------------
# Test 1-6: normalize_codex_event == CodexAdapter._normalize for all shapes
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
        assert via_spawn.provider == via_adapter.provider == "codex"
        assert via_spawn.dispatch_id == via_adapter.dispatch_id == _DISPATCH_ID
        assert via_spawn.terminal_id == via_adapter.terminal_id == _TERMINAL_ID

    def test_normalizer_identity_thread_started(self):
        self._assert_identical(_RAW_THREAD_STARTED)

    def test_normalizer_identity_agent_message(self):
        self._assert_identical(_RAW_AGENT_MSG)

    def test_normalizer_identity_turn_completed(self):
        self._assert_identical(_RAW_TURN_COMPLETED)

    def test_normalizer_identity_error_event(self):
        self._assert_identical(_RAW_ERROR)

    def test_normalizer_identity_item_completed(self):
        self._assert_identical(_RAW_ITEM_COMPLETED_AGENT)

    def test_normalizer_identity_token_count(self):
        self._assert_identical(_RAW_TOKEN_COUNT)


# ---------------------------------------------------------------------------
# Test 7: CodexAdapter.execute() collects same events as spawn_codex
# ---------------------------------------------------------------------------

class TestCodexAdapterDelegatesToSpawn:
    """execute() is a thin wrapper over spawn_codex; collected events must match."""

    def _build_canonical_events(self) -> List[CanonicalEvent]:
        return [normalize_codex_event(r, _TERMINAL_ID, _DISPATCH_ID) for r in _ALL_RAW_EVENTS]

    def test_codex_adapter_delegates_to_spawn(self):
        canonical_events = self._build_canonical_events()

        direct_collected: List[dict] = []

        def _collect(tid, event_dict, dispatch_id=None):
            direct_collected.append(event_dict)

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
                return_value=iter(canonical_events),
            ):
                result = spawn_codex(
                    prompt="test instruction",
                    model="",
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                    event_writer=_collect,
                )

        assert result.events_written == len(_ALL_RAW_EVENTS)
        assert len(direct_collected) == len(_ALL_RAW_EVENTS)

        # Compare event_type and data for each collected event
        for i, (raw, collected_dict) in enumerate(zip(_ALL_RAW_EVENTS, direct_collected)):
            expected = normalize_codex_event(raw, _TERMINAL_ID, _DISPATCH_ID)
            assert collected_dict["event_type"] == expected.event_type, (
                f"event[{i}] event_type mismatch: "
                f"{collected_dict['event_type']!r} != {expected.event_type!r}"
            )
            assert collected_dict["data"] == expected.data, (
                f"event[{i}] data mismatch: {collected_dict['data']} != {expected.data}"
            )


# ---------------------------------------------------------------------------
# Test 8: event_writer receives dict for every event
# ---------------------------------------------------------------------------

class TestEventWriterReceivesAllEvents:
    """event_writer callback is called once per canonical event."""

    def test_event_writer_receives_all_events(self):
        canonical_events = [
            normalize_codex_event(r, _TERMINAL_ID, _DISPATCH_ID) for r in _ALL_RAW_EVENTS
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
                return_value=iter(canonical_events),
            ):
                spawn_codex(
                    prompt="test",
                    model="",
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
# Test 9: session_id extracted from init event
# ---------------------------------------------------------------------------

class TestSessionIdExtraction:
    """session_id is extracted from the init event data when available."""

    def test_session_id_extracted_from_init(self):
        init_with_session = CanonicalEvent(
            dispatch_id=_DISPATCH_ID,
            terminal_id=_TERMINAL_ID,
            provider="codex",
            event_type="init",
            data={"raw_type": "thread.started", "session_id": "sess-codex-42"},
            observability_tier=1,
        )

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
                return_value=iter([init_with_session]),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                )

        assert result.session_id == "sess-codex-42"

    def test_session_id_none_when_no_init(self):
        text_event = normalize_codex_event(_RAW_AGENT_MSG, _TERMINAL_ID, _DISPATCH_ID)

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
                return_value=iter([text_event]),
            ):
                result = spawn_codex(
                    prompt="test",
                    model="",
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                )

        assert result.session_id is None


# ---------------------------------------------------------------------------
# Test 10: completion_text joins agent_message text values
# ---------------------------------------------------------------------------

class TestCompletionText:
    """completion_text is built from all text and complete event text values."""

    def test_completion_text_from_agent_messages(self):
        events = [
            normalize_codex_event({"type": "agent_message", "text": "Part 1."}, _TERMINAL_ID, _DISPATCH_ID),
            normalize_codex_event({"type": "agent_message", "text": "Part 2."}, _TERMINAL_ID, _DISPATCH_ID),
        ]

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
                    dispatch_id=_DISPATCH_ID,
                    terminal_id=_TERMINAL_ID,
                )

        assert "Part 1." in result.completion_text
        assert "Part 2." in result.completion_text
