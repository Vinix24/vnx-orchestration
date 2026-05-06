#!/usr/bin/env python3
"""Unit tests for CodexAdapter._normalize() — Codex NDJSON → CanonicalEvent mapping.

Tests every documented Codex event type and verifies correct CanonicalEvent
output, including observability_tier=1 on all events.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.codex_adapter import CodexAdapter
from canonical_event import CanonicalEvent


@pytest.fixture()
def adapter() -> CodexAdapter:
    """CodexAdapter with current context pre-set for _normalize tests."""
    a = CodexAdapter("T1")
    a._current_terminal_id = "T1"
    a._current_dispatch_id = "test-dispatch-001"
    return a


def assert_canonical(
    event: CanonicalEvent,
    expected_type: str,
    *,
    tier: int = 1,
    provider: str = "codex",
) -> None:
    assert event.event_type == expected_type, f"Expected type={expected_type!r}, got {event.event_type!r}"
    assert event.observability_tier == tier, f"Expected tier={tier}, got {event.observability_tier}"
    assert event.provider == provider
    assert event.dispatch_id == "test-dispatch-001"
    assert event.terminal_id == "T1"


# ── thread.started / session_start → init ────────────────────────────────────

class TestInitEvents:
    def test_thread_started(self, adapter):
        raw = {"type": "thread.started"}
        event = adapter._normalize(raw)
        assert_canonical(event, "init")
        assert event.data["raw_type"] == "thread.started"

    def test_session_start_via_event_msg_payload(self, adapter):
        raw = {"event_msg": {"payload": {"type": "session_start"}}}
        event = adapter._normalize(raw)
        assert_canonical(event, "init")
        assert event.data["raw_type"] == "session_start"

    def test_session_start_direct(self, adapter):
        raw = {"type": "session_start"}
        event = adapter._normalize(raw)
        assert_canonical(event, "init")


# ── agent_message → text ──────────────────────────────────────────────────────

class TestTextEvents:
    def test_direct_agent_message_text_field(self, adapter):
        raw = {"type": "agent_message", "text": "Found 3 issues."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Found 3 issues."

    def test_direct_agent_message_content_field(self, adapter):
        raw = {"type": "agent_message", "content": "Review complete."}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Review complete."

    def test_agent_message_via_event_msg_payload(self, adapter):
        raw = {"event_msg": {"payload": {"type": "agent_message", "text": "Analyzing..."}}}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Analyzing..."

    def test_item_completed_agent_message_string_content(self, adapter):
        raw = {"type": "item.completed", "item": {"type": "agent_message", "content": "Done."}}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == "Done."

    def test_item_completed_agent_message_list_content(self, adapter):
        raw = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [
                    {"type": "text", "text": "Part one."},
                    {"type": "text", "text": "Part two."},
                ],
            },
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert "Part one." in event.data["text"]
        assert "Part two." in event.data["text"]

    def test_item_completed_agent_message_empty_content(self, adapter):
        raw = {"type": "item.completed", "item": {"type": "agent_message", "content": ""}}
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == ""


# ── command_execution → tool_use / tool_result ────────────────────────────────

class TestCommandExecutionEvents:
    def test_item_started_command_execution(self, adapter):
        raw = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "ls -la"},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["command"] == "ls -la"
        assert event.data["raw_type"] == "item.started"

    def test_item_updated_command_execution(self, adapter):
        raw = {
            "type": "item.updated",
            "item": {"type": "command_execution", "cmd": "pytest tests/"},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert event.data["command"] == "pytest tests/"
        assert event.data["raw_type"] == "item.updated"

    def test_item_started_command_as_list(self, adapter):
        raw = {
            "type": "item.started",
            "item": {"type": "command_execution", "args": ["git", "diff", "--stat"]},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_use")
        assert "git" in event.data["command"]

    def test_item_completed_command_execution(self, adapter):
        raw = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "output": "total 42\n-rw-r--r-- 1 user user 100 Jan 1 file.py",
                "exit_code": 0,
            },
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert "total 42" in event.data["output"]
        assert event.data["exit_code"] == 0

    def test_item_completed_command_execution_nonzero_exit(self, adapter):
        raw = {
            "type": "item.completed",
            "item": {"type": "command_execution", "output": "error", "exit_code": 1},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "tool_result")
        assert event.data["exit_code"] == 1


# ── error → error ─────────────────────────────────────────────────────────────

class TestErrorEvents:
    def test_error_with_message_field(self, adapter):
        raw = {"type": "error", "message": "Codex timeout"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert event.data["message"] == "Codex timeout"

    def test_error_via_event_msg_payload(self, adapter):
        raw = {"event_msg": {"payload": {"type": "error", "error": "rate limit exceeded"}}}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert "rate limit" in event.data["message"]

    def test_error_with_text_field(self, adapter):
        raw = {"type": "error", "text": "unknown command"}
        event = adapter._normalize(raw)
        assert_canonical(event, "error")
        assert event.data["message"] == "unknown command"


# ── turn.completed → complete (with token_count) ──────────────────────────────

class TestCompleteEvents:
    def test_turn_completed_with_token_count(self, adapter):
        raw = {
            "type": "turn.completed",
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 1200,
                    "output_tokens": 350,
                }
            },
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        assert event.data.get("token_count") is not None
        assert event.data["token_count"]["input_tokens"] == 1200
        assert event.data["token_count"]["output_tokens"] == 350

    def test_turn_completed_without_token_count(self, adapter):
        raw = {"type": "turn.completed"}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")

    def test_result_event_with_content(self, adapter):
        raw = {"type": "result", "content": "Review finished. No critical issues."}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        assert "Review finished" in event.data.get("text", "")

    def test_message_event(self, adapter):
        raw = {"type": "message", "text": "Summary complete."}
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")

    def test_result_with_usage_block(self, adapter):
        raw = {
            "type": "result",
            "content": "ok",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "complete")
        tc = event.data.get("token_count")
        assert tc is not None
        assert tc["input_tokens"] == 100
        assert tc["output_tokens"] == 40


# ── intermediate token_count → text (with token data) ────────────────────────

class TestIntermediateTokenCountEvents:
    def test_token_count_via_event_msg_payload(self, adapter):
        raw = {
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 500,
                    "output_tokens": 120,
                }
            }
        }
        event = adapter._normalize(raw)
        assert_canonical(event, "text")
        assert event.data["text"] == ""
        tc = event.data.get("token_count")
        assert tc is not None
        assert tc["input_tokens"] == 500
        assert tc["output_tokens"] == 120

    def test_token_count_with_cache_fields(self, adapter):
        raw = {
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 2048,
                    "cached_input_tokens": 512,
                    "output_tokens": 600,
                    "cache_creation_input_tokens": 0,
                }
            }
        }
        event = adapter._normalize(raw)
        assert event.event_type == "text"
        tc = event.data["token_count"]
        assert tc["cache_read_tokens"] == 512

    def test_zero_token_count_falls_through_to_unknown(self, adapter):
        raw = {
            "event_msg": {
                "payload": {"type": "token_count", "input_tokens": 0, "output_tokens": 0}
            }
        }
        event = adapter._normalize(raw)
        # Zero token count is not useful; falls through to unknown → error
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


# ── observability_tier is always 1 ───────────────────────────────────────────

class TestObservabilityTier:
    @pytest.mark.parametrize("raw,expected_type", [
        ({"type": "thread.started"}, "init"),
        ({"type": "agent_message", "text": "hi"}, "text"),
        ({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}, "tool_use"),
        ({"type": "item.completed", "item": {"type": "command_execution", "output": "ok", "exit_code": 0}}, "tool_result"),
        ({"type": "error", "message": "fail"}, "error"),
        ({"type": "turn.completed"}, "complete"),
    ])
    def test_all_events_have_tier_1(self, adapter, raw, expected_type):
        event = adapter._normalize(raw)
        assert event.observability_tier == 1, (
            f"Event type {expected_type!r} must have observability_tier=1, got {event.observability_tier}"
        )


# ── to_dict() round-trip ──────────────────────────────────────────────────────

class TestToDictRoundtrip:
    def test_normalize_to_dict_has_required_fields(self, adapter):
        raw = {"type": "agent_message", "text": "Hello"}
        event = adapter._normalize(raw)
        d = event.to_dict()
        assert d["event_type"] == "text"
        assert d["provider"] == "codex"
        assert d["observability_tier"] == 1
        assert d["dispatch_id"] == "test-dispatch-001"
        assert d["terminal_id"] == "T1"
        assert "event_id" in d
        assert "timestamp" in d
