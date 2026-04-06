#!/usr/bin/env python3
"""Tests for agent stream SSE endpoint (F29 PR-2).

Tests SSE response format, since-parameter reconnection, status endpoint,
and client disconnect handling.
"""

import io
import json
import os
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add dashboard and scripts/lib to path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

from event_store import EventStore
from api_agent_stream import handle_agent_stream, handle_agent_stream_status, _store


@pytest.fixture
def tmp_events_dir(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    return events_dir


@pytest.fixture
def store(tmp_events_dir):
    return EventStore(events_dir=tmp_events_dir)


def _make_handler(wfile=None):
    """Build a mock HTTP handler with response tracking."""
    handler = MagicMock()
    handler.wfile = wfile or io.BytesIO()
    headers_sent = {}

    def send_header(name, value):
        headers_sent[name] = value

    handler.send_header = MagicMock(side_effect=send_header)
    handler._headers_sent = headers_sent
    return handler


class TestSSEResponseFormat:
    def test_returns_event_stream_content_type(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {"session_id": "abc"}})
        handler = _make_handler()

        # Stop the loop after first flush by raising on second flush
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count > 1:
                raise BrokenPipeError("client disconnected")

        handler.wfile.flush = limited_flush

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "T1", None)

        handler.send_response.assert_called_with(HTTPStatus.OK)
        handler.send_header.assert_any_call("Content-Type", "text/event-stream")
        handler.send_header.assert_any_call("Cache-Control", "no-cache")
        handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")

    def test_events_formatted_as_sse_data_lines(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {"session_id": "abc"}})
        store.append("T1", {"type": "result", "data": {"text": "hello"}})

        handler = _make_handler()
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count > 1:
                raise BrokenPipeError()

        handler.wfile.flush = limited_flush

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "T1", None)

        output = handler.wfile.getvalue().decode("utf-8")
        lines = [l for l in output.split("\n") if l.startswith("data: ")]
        assert len(lines) == 2

        for line in lines:
            payload = json.loads(line[len("data: "):])
            assert "type" in payload
            assert "timestamp" in payload
            assert "terminal" in payload

    def test_returns_404_for_empty_terminal(self, store, tmp_events_dir):
        handler = _make_handler()

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "T2", None)

        handler.send_response.assert_called_with(HTTPStatus.NOT_FOUND)

    def test_returns_400_for_invalid_terminal(self, store, tmp_events_dir):
        handler = _make_handler()

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "INVALID", None)

        handler.send_response.assert_called_with(HTTPStatus.BAD_REQUEST)


class TestSinceReconnection:
    def test_since_filters_older_events(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})
        time.sleep(0.01)

        # Get timestamp of first event to use as since
        events = list(store.tail("T1"))
        first_ts = events[0]["timestamp"]

        time.sleep(0.01)
        store.append("T1", {"type": "result", "data": {"text": "new"}})

        handler = _make_handler()
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count > 1:
                raise BrokenPipeError()

        handler.wfile.flush = limited_flush

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "T1", since=first_ts)

        output = handler.wfile.getvalue().decode("utf-8")
        lines = [l for l in output.split("\n") if l.startswith("data: ")]
        assert len(lines) == 1

        payload = json.loads(lines[0][len("data: "):])
        assert payload["type"] == "result"


class TestStatusEndpoint:
    def test_status_lists_terminals_with_events(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})
        store.append("T3", {"type": "init", "data": {}})

        handler = _make_handler()

        with patch("api_agent_stream._store", store):
            handle_agent_stream_status(handler)

        handler.send_response.assert_called_with(HTTPStatus.OK)
        body = handler.wfile.getvalue().decode("utf-8")
        result = json.loads(body)

        assert "T1" in result["terminals"]
        assert "T3" in result["terminals"]
        assert "T0" not in result["terminals"]
        assert "T2" not in result["terminals"]

        for tid in ("T1", "T3"):
            info = result["terminals"][tid]
            assert info["event_count"] > 0
            assert "last_timestamp" in info

    def test_status_empty_when_no_events(self, store, tmp_events_dir):
        handler = _make_handler()

        with patch("api_agent_stream._store", store):
            handle_agent_stream_status(handler)

        body = handler.wfile.getvalue().decode("utf-8")
        result = json.loads(body)
        assert result["terminals"] == {}


class TestClientDisconnect:
    def test_broken_pipe_stops_cleanly(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})

        handler = _make_handler()

        def raise_on_write(data):
            raise BrokenPipeError("client gone")

        handler.wfile.write = raise_on_write

        with patch("api_agent_stream._store", store):
            # Should not raise — disconnect handled gracefully
            handle_agent_stream(handler, "T1", None)

    def test_connection_reset_stops_cleanly(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})

        handler = _make_handler()

        def raise_on_write(data):
            raise ConnectionResetError("reset")

        handler.wfile.write = raise_on_write

        with patch("api_agent_stream._store", store):
            handle_agent_stream(handler, "T1", None)


@pytest.fixture
def seeded_store(tmp_events_dir):
    """EventStore pre-loaded with all normalized event types."""
    es = EventStore(events_dir=tmp_events_dir)
    events = [
        {"type": "init", "data": {"session_id": "test-123"}},
        {"type": "thinking", "data": {"thinking": "Analyzing..."}},
        {"type": "tool_use", "data": {"name": "Read", "input": {"path": "test.py"}}},
        {"type": "tool_result", "data": {"output": "contents"}},
        {"type": "result", "data": {"result": "Done!"}},
    ]
    for ev in events:
        es.append("T1", ev, dispatch_id="test-001")
    return es


class TestNormalizedTypeSeeding:
    """Verify EventStore correctly stores all normalized event types with dispatch correlation."""

    def test_all_types_stored(self, seeded_store):
        events = list(seeded_store.tail("T1"))
        types = [e["type"] for e in events]
        assert types == ["init", "thinking", "tool_use", "tool_result", "result"]

    def test_dispatch_id_on_all_events(self, seeded_store):
        for ev in seeded_store.tail("T1"):
            assert ev["dispatch_id"] == "test-001"

    def test_sequence_numbers_monotonic(self, seeded_store):
        seqs = [e["sequence"] for e in seeded_store.tail("T1")]
        assert seqs == [1, 2, 3, 4, 5]

    def test_terminal_field_consistent(self, seeded_store):
        for ev in seeded_store.tail("T1"):
            assert ev["terminal"] == "T1"

    def test_data_payloads_preserved(self, seeded_store):
        events = list(seeded_store.tail("T1"))
        assert events[0]["data"]["session_id"] == "test-123"
        assert events[1]["data"]["thinking"] == "Analyzing..."
        assert events[2]["data"]["name"] == "Read"
        assert events[3]["data"]["output"] == "contents"
        assert events[4]["data"]["result"] == "Done!"

    def test_sse_streams_all_normalized_types(self, seeded_store):
        handler = _make_handler()
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count > 1:
                raise BrokenPipeError()

        handler.wfile.flush = limited_flush

        with patch("api_agent_stream._store", seeded_store):
            handle_agent_stream(handler, "T1", None)

        output = handler.wfile.getvalue().decode("utf-8")
        lines = [l for l in output.split("\n") if l.startswith("data: ")]
        assert len(lines) == 5

        types = [json.loads(l[len("data: "):])["type"] for l in lines]
        assert types == ["init", "thinking", "tool_use", "tool_result", "result"]


class TestArchiveAccessibility:
    """Verify archive NDJSON files are created and contain correct data."""

    def test_archive_creates_ndjson_file(self, seeded_store, tmp_events_dir):
        path = seeded_store.archive("T1", "test-001")
        assert path is not None
        assert path.exists()
        assert path.suffix == ".ndjson"
        assert path.name == "test-001.ndjson"

    def test_archive_preserves_all_events(self, seeded_store, tmp_events_dir):
        path = seeded_store.archive("T1", "test-001")
        lines = [l for l in path.read_text().strip().split("\n") if l]
        assert len(lines) == 5

    def test_archive_events_have_dispatch_id(self, seeded_store, tmp_events_dir):
        path = seeded_store.archive("T1", "test-001")
        for line in path.read_text().strip().split("\n"):
            ev = json.loads(line)
            assert ev["dispatch_id"] == "test-001"

    def test_archive_dir_matches_terminal(self, seeded_store, tmp_events_dir):
        seeded_store.archive("T1", "test-001")
        archive_dir = seeded_store.archive_dir("T1")
        assert archive_dir.exists()
        assert archive_dir.name == "T1"
        assert (archive_dir / "test-001.ndjson").exists()

    def test_clear_with_archive_preserves_data(self, seeded_store, tmp_events_dir):
        seeded_store.clear("T1", archive_dispatch_id="test-001")
        assert seeded_store.event_count("T1") == 0
        archive_path = seeded_store.archive_dir("T1") / "test-001.ndjson"
        assert archive_path.exists()
        lines = [l for l in archive_path.read_text().strip().split("\n") if l]
        assert len(lines) == 5

    def test_archive_empty_terminal_returns_none(self, store, tmp_events_dir):
        result = store.archive("T2", "no-events")
        assert result is None
