#!/usr/bin/env python3
"""test_canonical_event_shape_conformance.py — Wave 4.6 PR-4.6.6

Verifies that CanonicalEvent.from_provider_event() maps raw provider events to
valid canonical shape, and that EventStore.append() enforces the schema via
EventShapeError.

Tests:
  TestFromProviderEventClaude   — claude fixture event → all required fields
  TestFromProviderEventCodex    — codex fixture events → all required fields
  TestFromProviderEventGemini   — gemini fixture events → all required fields
  TestFromProviderEventLiteLLM  — litellm fixture events → all required fields
  TestEventShapeEnforcement     — EventShapeError raised on invalid shape
  TestProviderRestriction       — provider field restricted to VALID_PROVIDERS
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from canonical_event import (
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    CanonicalEvent,
    EventShapeError,
)
from event_store import EventStore


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _assert_required_fields(ev: CanonicalEvent, provider: str, dispatch_id: str, terminal_id: str) -> None:
    """Assert all required canonical fields are present and valid."""
    assert ev.dispatch_id == dispatch_id
    assert ev.terminal_id == terminal_id
    assert ev.provider == provider
    assert ev.event_type in VALID_EVENT_TYPES, f"Invalid event_type: {ev.event_type!r}"
    assert ev.schema_version == 1
    assert isinstance(ev.data, dict)
    assert ev.timestamp  # non-empty ISO string
    assert ev.event_id   # non-empty UUID string


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

class TestFromProviderEventClaude:
    """Claude fixture events produce valid CanonicalEvent with all required fields."""

    def test_text_event_required_fields(self):
        raw = {"type": "text", "data": {"text": "hello"}}
        ev = CanonicalEvent.from_provider_event("claude", raw, "d-claude-001", "T1")
        _assert_required_fields(ev, "claude", "d-claude-001", "T1")
        assert ev.event_type == "text"
        assert ev.data == {"text": "hello"}

    def test_init_event(self):
        raw = {"type": "init", "data": {"session_id": "sess-abc"}}
        ev = CanonicalEvent.from_provider_event("claude", raw, "d-001", "T1")
        assert ev.event_type == "init"

    def test_result_maps_to_complete(self):
        raw = {"type": "result", "data": {"output": "done"}}
        ev = CanonicalEvent.from_provider_event("claude", raw, "d-001", "T1")
        assert ev.event_type == "complete"

    def test_model_field_extracted_when_present(self):
        raw = {"type": "text", "data": {}, "model": "claude-sonnet-4-6"}
        ev = CanonicalEvent.from_provider_event("claude", raw, "d-001", "T1")
        assert ev.model == "claude-sonnet-4-6"

    def test_unknown_type_maps_to_error(self):
        raw = {"type": "something_weird", "data": {}}
        ev = CanonicalEvent.from_provider_event("claude", raw, "d-001", "T1")
        assert ev.event_type == "error"
        assert ev.provider_meta.get("legacy_type") == "something_weird"


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

class TestFromProviderEventCodex:
    """Codex fixture events produce valid CanonicalEvent with all required fields."""

    def test_agent_message_maps_to_text(self):
        raw = {"type": "agent_message", "text": "codex output"}
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-codex-001", "T1")
        _assert_required_fields(ev, "codex", "d-codex-001", "T1")
        assert ev.event_type == "text"
        assert ev.observability_tier == 1

    def test_thread_started_maps_to_init(self):
        raw = {"type": "thread.started"}
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-001", "T1")
        assert ev.event_type == "init"
        assert ev.data.get("raw_type") == "thread.started"

    def test_turn_completed_maps_to_complete(self):
        raw = {"type": "turn.completed"}
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-001", "T1")
        assert ev.event_type == "complete"

    def test_error_event(self):
        raw = {"type": "error", "message": "something went wrong"}
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-001", "T1")
        assert ev.event_type == "error"
        assert "wrong" in ev.data.get("message", "")

    def test_command_execution_tool_use(self):
        raw = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "ls -la"},
        }
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-001", "T1")
        assert ev.event_type == "tool_use"

    def test_all_required_fields_present(self):
        raw = {"type": "agent_message", "text": "x"}
        ev = CanonicalEvent.from_provider_event("codex", raw, "d-001", "T2")
        _assert_required_fields(ev, "codex", "d-001", "T2")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

class TestFromProviderEventGemini:
    """Gemini fixture events produce valid CanonicalEvent with all required fields."""

    def test_message_maps_to_text(self):
        raw = {"type": "message", "text": "gemini output"}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-gemini-001", "T1")
        _assert_required_fields(ev, "gemini", "d-gemini-001", "T1")
        assert ev.event_type == "text"
        assert ev.data.get("text") == "gemini output"

    def test_session_start_maps_to_init(self):
        raw = {"type": "session_start"}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-001", "T1")
        assert ev.event_type == "init"

    def test_result_maps_to_complete(self):
        raw = {"type": "result", "text": "final answer"}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-001", "T1")
        assert ev.event_type == "complete"
        assert ev.data.get("text") == "final answer"

    def test_tool_use_event(self):
        raw = {"type": "tool_use", "name": "run_code", "args": {"code": "print(1)"}}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-001", "T1")
        assert ev.event_type == "tool_use"
        assert ev.data.get("name") == "run_code"

    def test_error_event(self):
        raw = {"type": "error", "message": "quota exceeded"}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-001", "T1")
        assert ev.event_type == "error"

    def test_all_required_fields_present(self):
        raw = {"type": "message", "text": "x"}
        ev = CanonicalEvent.from_provider_event("gemini", raw, "d-001", "T3")
        _assert_required_fields(ev, "gemini", "d-001", "T3")


# ---------------------------------------------------------------------------
# LiteLLM
# ---------------------------------------------------------------------------

class TestFromProviderEventLiteLLM:
    """LiteLLM fixture events produce valid CanonicalEvent with all required fields."""

    def test_text_chunk_maps_to_text(self):
        raw = {
            "choices": [{"delta": {"content": "litellm output"}, "finish_reason": None}],
            "model": "deepseek/deepseek-chat",
        }
        ev = CanonicalEvent.from_provider_event("litellm", raw, "d-litellm-001", "T1")
        _assert_required_fields(ev, "litellm", "d-litellm-001", "T1")
        assert ev.event_type == "text"
        assert ev.data.get("content") == "litellm output"

    def test_assistant_role_maps_to_init(self):
        raw = {
            "choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            "model": "deepseek/deepseek-chat",
        }
        ev = CanonicalEvent.from_provider_event("litellm", raw, "d-001", "T1")
        assert ev.event_type == "init"

    def test_stop_finish_reason_maps_to_complete(self):
        raw = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "model": "anthropic/claude-haiku",
        }
        ev = CanonicalEvent.from_provider_event("litellm", raw, "d-001", "T1")
        assert ev.event_type == "complete"
        assert ev.data.get("finish_reason") == "stop"

    def test_error_type_maps_to_error(self):
        raw = {"error_type": "AuthenticationError", "message": "bad key"}
        ev = CanonicalEvent.from_provider_event("litellm", raw, "d-001", "T1")
        assert ev.event_type == "error"
        assert ev.data.get("error_type") == "AuthenticationError"

    def test_sub_provider_passed_through(self):
        raw = {"choices": [{"delta": {"content": "x"}}]}
        ev = CanonicalEvent.from_provider_event(
            "litellm", raw, "d-001", "T1", sub_provider="deepseek"
        )
        assert ev.sub_provider == "deepseek"

    def test_all_required_fields_present(self):
        raw = {"choices": [{"delta": {"content": "x"}}]}
        ev = CanonicalEvent.from_provider_event("litellm", raw, "d-001", "T2")
        _assert_required_fields(ev, "litellm", "d-001", "T2")


# ---------------------------------------------------------------------------
# EventShapeError enforcement
# ---------------------------------------------------------------------------

class TestEventShapeEnforcement:
    """EventShapeError raised for schema violations; EventStore enforces on append."""

    def test_invalid_schema_version_raises(self):
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={}, schema_version=99,
        )
        with pytest.raises(EventShapeError, match="schema_version"):
            ev.validate_shape()

    def test_negative_tokens_input_raises(self):
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={}, tokens_input=-5,
        )
        with pytest.raises(EventShapeError, match="tokens_input"):
            ev.validate_shape()

    def test_negative_tokens_output_raises(self):
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={}, tokens_output=-1,
        )
        with pytest.raises(EventShapeError, match="tokens_output"):
            ev.validate_shape()

    def test_valid_token_counts_do_not_raise(self):
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={},
            tokens_input=1000, tokens_output=200,
            tokens_cache_read=50, tokens_cache_write=10,
        )
        ev.validate_shape()  # must not raise

    def test_none_token_counts_do_not_raise(self):
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={},
        )
        ev.validate_shape()  # must not raise

    def test_event_store_raises_event_shape_error_on_invalid_event(self, tmp_path: Path):
        store = EventStore(events_dir=tmp_path / "events")
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="claude",
            event_type="text", data={}, schema_version=42,
        )
        with pytest.raises(EventShapeError):
            store.append("T1", ev)

    def test_event_store_accepts_valid_canonical_event(self, tmp_path: Path):
        store = EventStore(events_dir=tmp_path / "events")
        ev = CanonicalEvent(
            dispatch_id="d", terminal_id="T1", provider="codex",
            event_type="text", data={"text": "ok"}, observability_tier=1,
        )
        store.append("T1", ev)  # must not raise
        assert store.event_count("T1") == 1


# ---------------------------------------------------------------------------
# Provider restriction
# ---------------------------------------------------------------------------

class TestProviderRestriction:
    """provider field is restricted to VALID_PROVIDERS at construction and in from_provider_event."""

    def test_valid_providers_accepted_in_from_provider_event(self):
        for provider in VALID_PROVIDERS:
            raw: Dict[str, Any] = {}
            # Each call should produce a valid CanonicalEvent (may be "error" type)
            ev = CanonicalEvent.from_provider_event(provider, raw, "d-001", "T1")
            assert ev.provider == provider

    def test_invalid_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="provider"):
            CanonicalEvent.from_provider_event("openai", {}, "d-001", "T1")

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="provider"):
            CanonicalEvent.from_provider_event("anthropic", {}, "d-001", "T1")

    def test_provider_field_in_canonical_event_validated(self):
        with pytest.raises(ValueError, match="provider"):
            CanonicalEvent(
                dispatch_id="d", terminal_id="T1", provider="invalid_provider",
                event_type="text", data={},
            )
