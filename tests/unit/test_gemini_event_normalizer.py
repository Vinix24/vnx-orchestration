#!/usr/bin/env python3
"""Unit tests for GeminiAdapter._normalize() — Gemini stream-json → CanonicalEvent mapping.

Tests every documented Gemini event type and verifies correct CanonicalEvent
output, including observability_tier=1 on all events (streaming path).

Also verifies that VNX_GEMINI_STREAM=0 default path preserves legacy Tier-3 behavior.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.gemini_adapter import GeminiAdapter, _gemini_stream_enabled
from canonical_event import CanonicalEvent


@pytest.fixture()
def adapter() -> GeminiAdapter:
    """GeminiAdapter with current context pre-set for _normalize tests."""
    a = GeminiAdapter("T3")
    a._current_terminal_id = "T3"
    a._current_dispatch_id = "test-gemini-dispatch-001"
    return a


def assert_canonical(
    event: CanonicalEvent,
    expected_type: str,
    *,
    tier: int = 1,
    provider: str = "gemini",
) -> None:
    assert event.event_type == expected_type, (
        f"Expected type={expected_type!r}, got {event.event_type!r}"
    )
    assert event.observability_tier == tier, (
        f"Expected tier={tier}, got {event.observability_tier}"
    )
    assert event.provider == provider
    assert event.dispatch_id == "test-gemini-dispatch-001"
    assert event.terminal_id == "T3"


# ── init events ──────────────────────────────────────────────────────────────

class TestInitEvents:
    def test_session_start(self, adapter):
        raw = {"type": "session_start"}
        event = adapter._normalize(raw)
        assert_canonical(event, "init")
        assert event.data["raw_type"] == "session_start"

    def test_init_type(self, adapter):
        raw = {"type": "init"}
        event = adapter._normalize(raw)
        assert_canonical(event, "init")
        assert event.data["raw_type"] == "init"


# ── text / message events ─────────────────────────────────────────────────────

class TestTextEvents:
    def test_message_with_text_field(self, adapter):
        raw = {"type": "message", "text": "Found 2 issues."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Found 2 issues."

    def test_text_type_with_text_field(self, adapter):
        raw = {"type": "text", "text": "Analysis complete."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Analysis complete."

    def test_content_type(self, adapter):
        raw = {"type": "content", "content": "Summary here."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Summary here."

    def test_message_with_content_field(self, adapter):
        raw = {"type": "message", "content": "Fallback content."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Fallback content."

    def test_message_empty_text(self, adapter):
        raw = {"type": "message", "text": ""}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == ""

    def test_message_with_message_field(self, adapter):
        raw = {"type": "message", "message": "Inline message field."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Inline message field."


# ── tool_use events ───────────────────────────────────────────────────────────

class TestToolUseEvents:
    def test_tool_use_with_name_and_args(self, adapter):
        raw = {"type": "tool_use", "name": "read_file", "args": {"path": "README.md"}}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["name"] == "read_file"
        assert event.data["args"] == {"path": "README.md"}

    def test_tool_call_with_function_name(self, adapter):
        raw = {"type": "tool_call", "function_name": "list_files", "input": {"dir": "."}}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["name"] == "list_files"

    def test_function_call_type(self, adapter):
        raw = {"type": "function_call", "name": "search", "args": {"query": "test"}}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["name"] == "search"

    def test_tool_use_args_not_dict(self, adapter):
        raw = {"type": "tool_use", "name": "run", "args": "some_string_args"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert isinstance(event.data["args"], dict)

    def test_tool_use_missing_name(self, adapter):
        raw = {"type": "tool_use"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["name"] == ""

    def test_tool_use_with_tool_field(self, adapter):
        raw = {"type": "tool_use", "tool": "bash"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["name"] == "bash"


# ── tool_result events ────────────────────────────────────────────────────────

class TestToolResultEvents:
    def test_tool_result_with_output(self, adapter):
        raw = {"type": "tool_result", "output": "file contents here"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert event.data["output"] == "file contents here"

    def test_tool_response_type(self, adapter):
        raw = {"type": "tool_response", "result": "search results"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert event.data["output"] == "search results"

    def test_function_response_type(self, adapter):
        raw = {"type": "function_response", "content": "function output"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert event.data["output"] == "function output"

    def test_tool_result_empty_output(self, adapter):
        raw = {"type": "tool_result"}
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert event.data["output"] == ""


# ── complete / result events ──────────────────────────────────────────────────

class TestCompleteEvents:
    def test_result_with_text(self, adapter):
        raw = {"type": "result", "text": "Review done. No blockers."}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        assert "Review done" in event.data.get("text", "")

    def test_done_type(self, adapter):
        raw = {"type": "done"}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")

    def test_complete_type(self, adapter):
        raw = {"type": "complete", "content": "Summary complete."}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        assert "Summary complete" in event.data.get("text", "")

    def test_finish_type(self, adapter):
        raw = {"type": "finish"}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")

    def test_result_with_usage_metadata(self, adapter):
        raw = {
            "type": "result",
            "text": "done",
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 350},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        tc = event.data.get("token_count")
        assert tc is not None
        assert tc["input_tokens"] == 1200
        assert tc["output_tokens"] == 350

    def test_result_with_output_field(self, adapter):
        raw = {"type": "result", "output": "output text"}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        assert event.data.get("text") == "output text"


# ── error events ──────────────────────────────────────────────────────────────

class TestErrorEvents:
    def test_error_with_message(self, adapter):
        raw = {"type": "error", "message": "rate limit exceeded"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert event.data["message"] == "rate limit exceeded"

    def test_error_with_error_field(self, adapter):
        raw = {"type": "error", "error": "timeout"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert "timeout" in event.data["message"]

    def test_error_with_text_field(self, adapter):
        raw = {"type": "error", "text": "unknown error"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert "unknown error" in event.data["message"]

    def test_error_no_message_field(self, adapter):
        raw = {"type": "error"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert "message" in event.data


# ── usageMetadata mid-stream ──────────────────────────────────────────────────

class TestUsageMetadataEvents:
    def test_usage_metadata_mid_stream(self, adapter):
        raw = {"usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 120}}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == ""
        tc = event.data.get("token_count")
        assert tc is not None
        assert tc["input_tokens"] == 500
        assert tc["output_tokens"] == 120

    def test_zero_usage_metadata_falls_through(self, adapter):
        raw = {"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0}}
        event = adapter._normalize(raw)
        # Zero counts are ignored; falls to unknown → error
        assert event.event_type == "error"


# ── unknown event type → error ────────────────────────────────────────────────

class TestUnknownEvents:
    def test_unknown_type_produces_error(self, adapter):
        raw = {"type": "some_future_event", "data": "value"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert "some_future_event" in event.data.get("raw_type", "")

    def test_empty_event_produces_error(self, adapter):
        raw = {}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")

    def test_event_with_only_id(self, adapter):
        raw = {"id": "evt-123"}
        event = adapter._normalize(raw)
        assert event.event_type == "error"


# ── observability_tier is always 1 (streaming path) ──────────────────────────

class TestObservabilityTier:
    @pytest.mark.parametrize("raw,expected_type", [
        ({"type": "session_start"}, "init"),
        ({"type": "message", "text": "hi"}, "text"),
        ({"type": "tool_use", "name": "read_file", "args": {}}, "tool_use"),
        ({"type": "tool_result", "output": "ok"}, "tool_result"),
        ({"type": "error", "message": "fail"}, "error"),
        ({"type": "result"}, "complete"),
    ])
    def test_all_events_have_tier_1(self, adapter, raw, expected_type):
        event = adapter._normalize(raw)
        assert event.observability_tier == 1, (
            f"Event type {expected_type!r} must have observability_tier=1, "
            f"got {event.observability_tier}"
        )


# ── to_dict() round-trip ──────────────────────────────────────────────────────

class TestToDictRoundtrip:
    def test_normalize_to_dict_has_required_fields(self, adapter):
        raw = {"type": "message", "text": "Hello"}
        event = adapter._normalize(raw)
        d = event.to_dict()
        assert d["event_type"] == "text"
        assert d["provider"] == "gemini"
        assert d["observability_tier"] == 1
        assert d["dispatch_id"] == "test-gemini-dispatch-001"
        assert d["terminal_id"] == "T3"
        assert "event_id" in d
        assert "timestamp" in d


# ── env-gate: VNX_GEMINI_STREAM flag ─────────────────────────────────────────

class TestEnvGate:
    def test_stream_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("VNX_GEMINI_STREAM", raising=False)
        assert _gemini_stream_enabled() is False

    def test_stream_enabled_when_1(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        assert _gemini_stream_enabled() is True

    def test_stream_disabled_when_0(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "0")
        assert _gemini_stream_enabled() is False

    def test_stream_disabled_when_empty(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "")
        assert _gemini_stream_enabled() is False

    def test_stream_disabled_when_false(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "false")
        assert _gemini_stream_enabled() is False


# ── legacy path: stream_events yields Tier-3 result ──────────────────────────

class TestLegacyStreamEvents:
    def test_stream_events_legacy_yields_single_result(self, monkeypatch, tmp_path):
        """VNX_GEMINI_STREAM=0: stream_events() yields a single Tier-3 result event."""
        import subprocess as sp

        monkeypatch.delenv("VNX_GEMINI_STREAM", raising=False)

        # Mock _execute_legacy to avoid needing real gemini binary
        from provider_adapter import AdapterResult

        adapter = GeminiAdapter("T3")
        adapter._current_terminal_id = "T3"
        adapter._current_dispatch_id = "legacy-test-001"

        fake_result = AdapterResult(
            status="done",
            output="legacy findings",
            events=[{"type": "result", "data": "legacy findings"}],
            event_count=1,
            duration_seconds=1.0,
            committed=False,
            commit_hash=None,
            report_path=None,
            provider="gemini",
            model="gemini-2.5-flash",
        )
        adapter._execute_legacy = lambda **_kw: fake_result

        events = list(adapter.stream_events("test instruction", {}))
        assert len(events) == 1
        assert events[0]["type"] == "result"
        assert events[0]["observability_tier"] == 3
