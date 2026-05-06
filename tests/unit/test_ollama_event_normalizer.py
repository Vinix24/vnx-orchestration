#!/usr/bin/env python3
"""Unit tests for OllamaAdapter._normalize_ollama_event().

Validates that every Ollama HTTP-stream chunk type maps to the correct
CanonicalEvent shape, observability tier, and data fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "lib"
sys.path.insert(0, str(_LIB_DIR))
sys.path.insert(0, str(_LIB_DIR / "adapters"))

from adapters.ollama_adapter import OllamaAdapter, _TIER_BASELINE, _TIER_FULL
from canonical_event import CanonicalEvent

_normalize = OllamaAdapter._normalize_ollama_event


# ---------------------------------------------------------------------------
# /api/generate format — text tokens
# ---------------------------------------------------------------------------

class TestGenerateApiTextTokens:
    def test_generate_token_is_text_event(self):
        ev = _normalize({"response": "hello", "done": False})
        assert ev.event_type == "text"

    def test_generate_token_data_contains_text(self):
        ev = _normalize({"response": "world", "done": False})
        assert ev.data["text"] == "world"

    def test_generate_token_tier_is_baseline(self):
        ev = _normalize({"response": "x", "done": False})
        assert ev.observability_tier == _TIER_BASELINE

    def test_generate_empty_token_is_text_event(self):
        ev = _normalize({"response": "", "done": False})
        assert ev.event_type == "text"
        assert ev.data["text"] == ""

    def test_generate_done_true_is_complete_event(self):
        ev = _normalize({"response": "last", "done": True})
        assert ev.event_type == "complete"

    def test_generate_done_carries_text(self):
        ev = _normalize({"response": "final token", "done": True})
        assert ev.data.get("text") == "final token"

    def test_generate_done_carries_token_count(self):
        ev = _normalize({"response": "", "done": True, "eval_count": 42})
        assert ev.data["token_count"] == 42

    def test_generate_done_without_eval_count(self):
        ev = _normalize({"response": "", "done": True})
        assert "token_count" not in ev.data
        assert ev.data["done"] is True

    def test_generate_done_empty_text_not_in_data(self):
        ev = _normalize({"response": "", "done": True, "eval_count": 5})
        assert "text" not in ev.data


# ---------------------------------------------------------------------------
# /api/chat format — message field
# ---------------------------------------------------------------------------

class TestChatApiTokens:
    def test_chat_message_is_text_event(self):
        ev = _normalize({"message": {"role": "assistant", "content": "hi"}, "done": False})
        assert ev.event_type == "text"

    def test_chat_message_content_in_data(self):
        ev = _normalize({"message": {"role": "assistant", "content": "stream"}, "done": False})
        assert ev.data["text"] == "stream"

    def test_chat_message_tier_is_baseline(self):
        ev = _normalize({"message": {"role": "assistant", "content": "x"}, "done": False})
        assert ev.observability_tier == _TIER_BASELINE

    def test_chat_done_is_complete_event(self):
        ev = _normalize({"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 10})
        assert ev.event_type == "complete"

    def test_chat_done_token_count(self):
        ev = _normalize({"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 10})
        assert ev.data["token_count"] == 10

    def test_chat_done_content_as_text(self):
        ev = _normalize({"message": {"role": "assistant", "content": "end"}, "done": True})
        assert ev.data.get("text") == "end"


# ---------------------------------------------------------------------------
# Tool-use detection — Tier-1
# ---------------------------------------------------------------------------

class TestToolUseDetection:
    _tool_calls = [{"function": {"name": "search", "arguments": {"q": "ollama"}}}]

    def test_tool_calls_in_message_yields_tool_use_event(self):
        raw = {
            "message": {"role": "assistant", "content": "", "tool_calls": self._tool_calls},
            "done": False,
        }
        ev = _normalize(raw)
        assert ev.event_type == "tool_use"

    def test_tool_use_tier_is_full(self):
        raw = {
            "message": {"role": "assistant", "content": "", "tool_calls": self._tool_calls},
            "done": False,
        }
        ev = _normalize(raw)
        assert ev.observability_tier == _TIER_FULL

    def test_tool_calls_in_data(self):
        raw = {
            "message": {"role": "assistant", "content": "", "tool_calls": self._tool_calls},
            "done": False,
        }
        ev = _normalize(raw)
        assert ev.data["tool_calls"] == self._tool_calls

    def test_done_with_tool_calls_complete_tier_1(self):
        raw = {
            "message": {"role": "assistant", "content": "", "tool_calls": self._tool_calls},
            "done": True,
            "eval_count": 7,
        }
        ev = _normalize(raw)
        assert ev.event_type == "complete"
        assert ev.observability_tier == _TIER_FULL
        assert ev.data["token_count"] == 7

    def test_no_tool_calls_is_tier_2(self):
        raw = {"message": {"role": "assistant", "content": "plain text"}, "done": False}
        ev = _normalize(raw)
        assert ev.observability_tier == _TIER_BASELINE


# ---------------------------------------------------------------------------
# Provider / identity fields
# ---------------------------------------------------------------------------

class TestProviderFields:
    def test_provider_is_ollama(self):
        ev = _normalize({"response": "x", "done": False})
        assert ev.provider == "ollama"

    def test_dispatch_id_passed_through(self):
        ev = _normalize({"response": "x", "done": False}, dispatch_id="d-999")
        assert ev.dispatch_id == "d-999"

    def test_terminal_id_passed_through(self):
        ev = _normalize({"response": "x", "done": False}, terminal_id="T3")
        assert ev.terminal_id == "T3"

    def test_defaults_empty_strings(self):
        ev = _normalize({"response": "x", "done": False})
        assert ev.dispatch_id == ""
        assert ev.terminal_id == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_raw_dict_is_text_event(self):
        ev = _normalize({})
        assert ev.event_type == "text"
        assert ev.data["text"] == ""

    def test_done_false_without_response_or_message(self):
        ev = _normalize({"done": False, "model": "gemma3:27b"})
        assert ev.event_type == "text"

    def test_eval_count_cast_to_int(self):
        ev = _normalize({"done": True, "eval_count": "100"})
        assert ev.data["token_count"] == 100

    def test_returns_canonical_event_instance(self):
        ev = _normalize({"response": "token", "done": False})
        assert isinstance(ev, CanonicalEvent)

    def test_message_none_does_not_crash(self):
        ev = _normalize({"message": None, "done": False})
        assert ev.event_type == "text"
