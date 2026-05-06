#!/usr/bin/env python3
"""Integration tests for OllamaAdapter HTTP streaming (requires running Ollama daemon).

Tests are skipped gracefully when no local Ollama daemon is reachable OR when
no models are installed.  Run with:

    pytest tests/integration/test_ollama_streaming.py -v

Requires a small model to be pulled, e.g.:
    ollama pull qwen2.5-coder:0.5b
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional

import pytest

_LIB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "lib"
sys.path.insert(0, str(_LIB_DIR))
sys.path.insert(0, str(_LIB_DIR / "adapters"))

from adapters.ollama_adapter import OllamaAdapter, _TIER_BASELINE, _TIER_FULL
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Daemon / model availability helpers
# ---------------------------------------------------------------------------

def _get_available_model() -> Optional[str]:
    """Return first available model name, or None if daemon down or no models."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return models[0] if models else None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


_AVAILABLE_MODEL = _get_available_model()

pytestmark = pytest.mark.skipif(
    _AVAILABLE_MODEL is None,
    reason=(
        "Ollama daemon not reachable on localhost:11434 or no models installed. "
        "Pull a model first: ollama pull qwen2.5-coder:0.5b"
    ),
)

_SIMPLE_PROMPT = "Reply with exactly one word: pong"


@pytest.fixture(autouse=True)
def set_test_model(monkeypatch):
    """Use the first available model so tests run regardless of which model is pulled."""
    if _AVAILABLE_MODEL:
        monkeypatch.setenv("VNX_OLLAMA_MODEL", _AVAILABLE_MODEL)


# ---------------------------------------------------------------------------
# Streaming produces CanonicalEvent objects
# ---------------------------------------------------------------------------

class TestStreamingProducesCanonicalEvents:
    def test_stream_events_yields_dicts(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter.stream_events(_SIMPLE_PROMPT, {}))
        assert len(events) > 0
        for ev in events:
            assert isinstance(ev, dict), f"expected dict, got {type(ev)}"

    def test_stream_events_has_complete_event(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter.stream_events(_SIMPLE_PROMPT, {}))
        types = [ev.get("event_type") for ev in events]
        assert "complete" in types, f"no complete event in {types}"

    def test_stream_events_complete_has_done_flag(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter.stream_events(_SIMPLE_PROMPT, {}))
        complete = next((e for e in events if e.get("event_type") == "complete"), None)
        assert complete is not None
        assert complete["data"].get("done") is True

    def test_drain_http_stream_yields_canonical_events(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-test-001",
            event_store=None,
        ))
        assert all(isinstance(e, CanonicalEvent) for e in events)

    def test_text_events_have_text_field(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-test-002",
            event_store=None,
        ))
        text_events = [e for e in events if e.event_type == "text"]
        for ev in text_events:
            assert "text" in ev.data


# ---------------------------------------------------------------------------
# Tier labeling
# ---------------------------------------------------------------------------

class TestTierLabeling:
    def test_text_only_model_is_tier_2(self):
        adapter = OllamaAdapter("T1")
        events = list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-tier-001",
            event_store=None,
        ))
        text_events = [e for e in events if e.event_type == "text"]
        assert len(text_events) > 0
        for ev in text_events:
            assert ev.observability_tier == _TIER_BASELINE, (
                f"expected tier {_TIER_BASELINE}, got {ev.observability_tier}"
            )


# ---------------------------------------------------------------------------
# EventStore live writes
# ---------------------------------------------------------------------------

class TestEventStoreLiveWrites:
    def test_events_written_to_store_during_streaming(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        adapter = OllamaAdapter("T1")
        list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-store-001",
            event_store=es,
        ))
        count = es.event_count("T1")
        assert count > 0, "EventStore received no events"

    def test_stored_events_have_correct_dispatch_id(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        adapter = OllamaAdapter("T1")
        list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-store-dispatch",
            event_store=es,
        ))
        stored = list(es.tail("T1"))
        assert all(e["dispatch_id"] == "int-store-dispatch" for e in stored)

    def test_stored_events_have_observability_tier(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        adapter = OllamaAdapter("T1")
        list(adapter._drain_http_stream(
            instruction=_SIMPLE_PROMPT,
            terminal_id="T1",
            dispatch_id="int-tier-store",
            event_store=es,
        ))
        stored = list(es.tail("T1"))
        for ev in stored:
            assert "observability_tier" in ev
            assert ev["observability_tier"] in (1, 2)


# ---------------------------------------------------------------------------
# execute() integration
# ---------------------------------------------------------------------------

class TestExecuteIntegration:
    def test_execute_returns_done(self):
        adapter = OllamaAdapter("T1")
        result = adapter.execute(_SIMPLE_PROMPT, {})
        assert result.status == "done"

    def test_execute_output_is_non_empty(self):
        adapter = OllamaAdapter("T1")
        result = adapter.execute(_SIMPLE_PROMPT, {})
        assert len(result.output) > 0

    def test_execute_events_include_complete(self):
        adapter = OllamaAdapter("T1")
        result = adapter.execute(_SIMPLE_PROMPT, {})
        types = [e.get("event_type") for e in result.events]
        assert "complete" in types

    def test_execute_complete_event_has_token_count(self):
        adapter = OllamaAdapter("T1")
        result = adapter.execute(_SIMPLE_PROMPT, {})
        complete = next((e for e in result.events if e.get("event_type") == "complete"), None)
        assert complete is not None
        assert "token_count" in complete["data"]
        assert complete["data"]["token_count"] > 0
