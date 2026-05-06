#!/usr/bin/env python3
"""Crash / negative integration tests for OllamaAdapter.

These tests verify error handling paths — connection refused, mid-stream
connection drop, malformed JSON — and do NOT require a running Ollama daemon.
They use mocks or deliberate bad configurations.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

_LIB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "lib"
sys.path.insert(0, str(_LIB_DIR))
sys.path.insert(0, str(_LIB_DIR / "adapters"))

from adapters.ollama_adapter import OllamaAdapter, _TIER_BASELINE
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_stream_response(lines: list[str]) -> "mock.MagicMock":
    """Return a mock urllib response that yields the given NDJSON lines."""
    encoded = b"".join((l.rstrip("\n") + "\n").encode() for l in lines)

    class _FakeResp:
        status = 200

        def __iter__(self):
            for line in encoded.split(b"\n"):
                if line:
                    yield line + b"\n"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return _FakeResp()


def _make_chunk(response: str = "", done: bool = False, eval_count: int | None = None) -> str:
    obj: dict = {"model": "gemma3:27b", "response": response, "done": done}
    if eval_count is not None:
        obj["eval_count"] = eval_count
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# Connection refused
# ---------------------------------------------------------------------------

class TestConnectionRefused:
    def test_drain_yields_error_event_on_url_error(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("connection refused")
            events = list(adapter._drain_http_stream(
                instruction="hello",
                terminal_id="T1",
                dispatch_id="neg-001",
            ))
        assert len(events) == 1
        assert events[0].event_type == "error"
        assert "connection failed" in events[0].data["reason"]

    def test_drain_yields_error_event_on_oserror(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = OSError("network unreachable")
            events = list(adapter._drain_http_stream(
                instruction="hello",
                terminal_id="T1",
                dispatch_id="neg-002",
            ))
        assert events[0].event_type == "error"

    def test_drain_yields_error_on_timeout(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = TimeoutError("timed out")
            events = list(adapter._drain_http_stream(
                instruction="hello",
                terminal_id="T1",
                dispatch_id="neg-003",
            ))
        assert events[0].event_type == "error"

    def test_execute_returns_failed_on_connection_refused(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("refused")
            result = adapter.execute("decide something", {})
        assert result.status == "failed"
        assert result.output == "ollama_unavailable"

    def test_stream_events_yields_error_dict_on_refusal(self):
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("refused")
            events = list(adapter.stream_events("prompt", {}))
        assert len(events) == 1
        assert events[0]["event_type"] == "error"


# ---------------------------------------------------------------------------
# Malformed JSON in stream
# ---------------------------------------------------------------------------

class TestMalformedJsonStream:
    def test_malformed_line_becomes_error_event(self):
        adapter = OllamaAdapter("T1")
        resp = _fake_stream_response([
            "this is not json",
            _make_chunk("hello", done=True, eval_count=5),
        ])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            events = list(adapter._drain_http_stream(
                instruction="test",
                terminal_id="T1",
                dispatch_id="neg-malformed-001",
            ))

        assert len(events) == 2
        assert events[0].event_type == "error"
        assert "raw" in events[0].data
        assert events[1].event_type == "complete"

    def test_malformed_line_reason_in_data(self):
        adapter = OllamaAdapter("T1")
        resp = _fake_stream_response(["not-valid-json"])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            events = list(adapter._drain_http_stream(
                instruction="test",
                terminal_id="T1",
                dispatch_id="neg-malformed-002",
            ))
        assert events[0].data.get("reason")

    def test_mixed_valid_and_malformed(self):
        adapter = OllamaAdapter("T1")
        resp = _fake_stream_response([
            _make_chunk("token1"),
            "bad line",
            _make_chunk("token2"),
            _make_chunk("", done=True, eval_count=3),
        ])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            events = list(adapter._drain_http_stream(
                instruction="test",
                terminal_id="T1",
                dispatch_id="neg-mixed-001",
            ))

        types = [e.event_type for e in events]
        assert "error" in types
        assert "text" in types
        assert "complete" in types


# ---------------------------------------------------------------------------
# Error events written to EventStore
# ---------------------------------------------------------------------------

class TestErrorEventsWrittenToStore:
    def test_connection_error_event_in_store(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        adapter = OllamaAdapter("T1")
        with mock.patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("refused")
            list(adapter._drain_http_stream(
                instruction="test",
                terminal_id="T1",
                dispatch_id="neg-store-001",
                event_store=es,
            ))

        stored = list(es.tail("T1"))
        assert len(stored) == 1
        assert stored[0]["type"] == "error"
        assert stored[0]["dispatch_id"] == "neg-store-001"

    def test_malformed_json_error_event_in_store(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        adapter = OllamaAdapter("T1")
        resp = _fake_stream_response(["not-json", _make_chunk("", done=True, eval_count=1)])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            list(adapter._drain_http_stream(
                instruction="test",
                terminal_id="T1",
                dispatch_id="neg-store-002",
                event_store=es,
            ))

        stored = list(es.tail("T1"))
        error_events = [e for e in stored if e["type"] == "error"]
        assert len(error_events) == 1


# ---------------------------------------------------------------------------
# Unreachable host — custom host env var
# ---------------------------------------------------------------------------

class TestUnreachableCustomHost:
    def test_unreachable_custom_host_returns_error_event(self):
        import os

        with mock.patch.dict(os.environ, {"VNX_OLLAMA_HOST": "http://192.0.2.1:11434"}):
            adapter = OllamaAdapter("T1")
            with mock.patch("urllib.request.urlopen") as m:
                m.side_effect = urllib.error.URLError("no route to host")
                events = list(adapter._drain_http_stream(
                    instruction="test",
                    terminal_id="T1",
                    dispatch_id="neg-custom-host",
                ))

        assert events[0].event_type == "error"
