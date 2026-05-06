"""Unit tests for LiteLLMAdapter._normalize() / _normalize_litellm_event().

No subprocess spawn required — all tests exercise the pure normalizer function
and the adapter's _normalize() instance method directly.
"""
from __future__ import annotations

import pytest

from adapters.litellm_adapter import LiteLLMAdapter, _normalize_litellm_event
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adapter() -> LiteLLMAdapter:
    a = LiteLLMAdapter("T2", litellm_model="anthropic/claude-sonnet-4-6")
    a._dispatch_id = "test-dispatch-001"
    return a


def _chunk(
    content: str = "",
    role: str = "",
    finish_reason: str | None = None,
    tool_calls: list | None = None,
    model: str = "gpt-4o",
) -> dict:
    """Build a minimal OpenAI SSE chunk dict."""
    delta: dict = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


# ---------------------------------------------------------------------------
# Tests — _normalize_litellm_event (module-level function)
# ---------------------------------------------------------------------------

class TestNormalizeLiteLLMEvent:
    def test_text_event_from_content_chunk(self):
        chunk = _chunk(content="Hello world")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "text"
        assert event.data["content"] == "Hello world"
        assert event.provider == "litellm"

    def test_init_event_from_first_assistant_chunk(self):
        chunk = _chunk(role="assistant", content="")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "init"
        assert event.data["model"] == "gpt-4o"

    def test_tool_use_event_from_tool_calls(self):
        tc = [{"index": 0, "id": "call_abc", "type": "function",
               "function": {"name": "read_file", "arguments": '{"path":'}}]
        chunk = _chunk(tool_calls=tc)
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "tool_use"
        assert event.data["tool_calls"] == tc

    def test_complete_event_from_stop_finish_reason(self):
        chunk = _chunk(finish_reason="stop")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "complete"
        assert event.data["finish_reason"] == "stop"

    def test_complete_event_from_tool_calls_finish_reason(self):
        chunk = _chunk(finish_reason="tool_calls")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "complete"

    def test_complete_event_from_length_finish_reason(self):
        chunk = _chunk(finish_reason="length")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "complete"

    def test_complete_event_from_end_turn_finish_reason(self):
        chunk = _chunk(finish_reason="end_turn")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "complete"

    def test_error_event_from_credentials_missing(self):
        chunk = {"error_type": "credentials_missing", "message": "No AWS creds"}
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "error"
        assert event.data["error_type"] == "credentials_missing"
        assert "No AWS creds" in event.data["message"]

    def test_error_event_from_runner_error(self):
        chunk = {"error_type": "runner_error", "message": "litellm not installed"}
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "error"

    def test_error_event_from_service_unavailable(self):
        chunk = {"error_type": "service_unavailable", "message": "connection refused"}
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "error"

    def test_text_event_from_empty_choices(self):
        chunk = {"id": "chatcmpl-x", "choices": []}
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "text"
        assert event.data["content"] == ""

    def test_text_event_when_choices_missing(self):
        chunk = {"id": "chatcmpl-x", "model": "gpt-4o"}
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "text"

    def test_dispatch_id_and_terminal_id_propagated(self):
        chunk = _chunk(content="hi")
        event = _normalize_litellm_event(chunk, "dispatch-xyz", "T3")
        assert event.dispatch_id == "dispatch-xyz"
        assert event.terminal_id == "T3"

    def test_observability_tier_is_1(self):
        chunk = _chunk(content="hi")
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.observability_tier == 1

    def test_tool_calls_takes_priority_over_finish_reason(self):
        tc = [{"index": 0, "id": "call_x", "type": "function",
               "function": {"name": "fn", "arguments": ""}}]
        chunk = _chunk(tool_calls=tc, finish_reason="tool_calls")
        # tool_calls in delta -> tool_use (takes priority)
        event = _normalize_litellm_event(chunk, "d1", "T1")
        assert event.event_type == "tool_use"


# ---------------------------------------------------------------------------
# Tests — LiteLLMAdapter._normalize() (instance method, uses instance state)
# ---------------------------------------------------------------------------

class TestAdapterNormalize:
    def test_normalize_binds_instance_dispatch_and_terminal(self):
        a = _adapter()
        chunk = _chunk(content="test")
        event = a._normalize(chunk)
        assert event.dispatch_id == "test-dispatch-001"
        assert event.terminal_id == "T2"

    def test_normalize_returns_canonical_event(self):
        a = _adapter()
        chunk = _chunk(content="x")
        event = a._normalize(chunk)
        assert isinstance(event, CanonicalEvent)

    def test_normalize_dispatch_id_can_be_updated(self):
        a = _adapter()
        a._dispatch_id = "new-dispatch-999"
        chunk = _chunk(content="y")
        event = a._normalize(chunk)
        assert event.dispatch_id == "new-dispatch-999"

    def test_normalize_error_chunk_produces_error_event(self):
        a = _adapter()
        chunk = {"error_type": "credentials_missing", "message": "test"}
        event = a._normalize(chunk)
        assert event.event_type == "error"


# ---------------------------------------------------------------------------
# Tests — LiteLLMAdapter metadata
# ---------------------------------------------------------------------------

class TestLiteLLMAdapterMeta:
    def test_name(self):
        assert LiteLLMAdapter("T1").name() == "litellm"

    def test_provider_name_class_attr(self):
        assert LiteLLMAdapter.provider_name == "litellm"

    def test_capabilities_include_code_and_review(self):
        from provider_adapter import Capability
        caps = LiteLLMAdapter("T1").capabilities()
        assert Capability.CODE in caps
        assert Capability.REVIEW in caps

    def test_capabilities_exclude_orchestrate(self):
        from provider_adapter import Capability
        caps = LiteLLMAdapter("T1").capabilities()
        assert not any(c.value == "orchestrate" for c in caps)

    def test_default_model_from_env(self, monkeypatch):
        monkeypatch.setenv("VNX_LITELLM_MODEL", "groq/llama-3.1-70b")
        a = LiteLLMAdapter("T1")
        assert a._litellm_model == "groq/llama-3.1-70b"

    def test_explicit_model_overrides_env(self, monkeypatch):
        monkeypatch.setenv("VNX_LITELLM_MODEL", "groq/llama-3.1-70b")
        a = LiteLLMAdapter("T1", litellm_model="bedrock/claude-sonnet-4-6")
        assert a._litellm_model == "bedrock/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Tests — adapter registry integration
# ---------------------------------------------------------------------------

class TestAdapterRegistryLiteLLM:
    def test_resolve_adapter_litellm_basic(self, monkeypatch):
        monkeypatch.setenv("VNX_PROVIDER_T1", "litellm")
        from adapters import resolve_adapter
        a = resolve_adapter("T1")
        assert isinstance(a, LiteLLMAdapter)
        assert a._litellm_model  # has some default

    def test_resolve_adapter_litellm_chain(self, monkeypatch):
        monkeypatch.setenv("VNX_PROVIDER_T1", "litellm/bedrock/claude-sonnet-4-6")
        from adapters import resolve_adapter
        a = resolve_adapter("T1")
        assert isinstance(a, LiteLLMAdapter)
        assert a._litellm_model == "bedrock/claude-sonnet-4-6"

    def test_resolve_adapter_litellm_groq_chain(self, monkeypatch):
        monkeypatch.setenv("VNX_PROVIDER_T1", "litellm/groq/llama-3.1-70b-versatile")
        from adapters import resolve_adapter
        a = resolve_adapter("T1")
        assert isinstance(a, LiteLLMAdapter)
        assert a._litellm_model == "groq/llama-3.1-70b-versatile"
