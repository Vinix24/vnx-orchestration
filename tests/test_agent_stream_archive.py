#!/usr/bin/env python3
"""Tests for archive endpoints in api_agent_stream."""

import json
import sys
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(DASHBOARD_DIR))


def _make_handler(wfile=None):
    handler = MagicMock()
    handler.wfile = wfile or BytesIO()
    return handler


def _capture_json(handler):
    handler.wfile.seek(0)
    raw = handler.wfile.read()
    return json.loads(raw)


def test_archive_list_returns_dispatch_ids(tmp_path, monkeypatch):
    import api_agent_stream as mod
    from event_store import EventStore

    store = EventStore(events_dir=tmp_path / "events")
    store.append("T1", {"type": "init", "data": {}}, dispatch_id="d-001")
    store.clear("T1", archive_dispatch_id="d-001")
    store.append("T1", {"type": "init", "data": {}}, dispatch_id="d-002")
    store.clear("T1", archive_dispatch_id="d-002")

    monkeypatch.setattr(mod, "_store", store)

    handler = _make_handler()
    mod.handle_agent_stream_archive_list(handler, "T1")

    body = handler.wfile.getvalue()
    result = json.loads(body)
    assert isinstance(result, list)
    dispatch_ids = {entry["dispatch_id"] for entry in result}
    assert dispatch_ids == {"d-001", "d-002"}
    for entry in result:
        assert "file_size" in entry
        assert "modified_at" in entry


def test_archive_returns_events(tmp_path, monkeypatch):
    import api_agent_stream as mod
    from event_store import EventStore

    store = EventStore(events_dir=tmp_path / "events")
    store.append("T2", {"type": "text", "data": {"text": "hello"}}, dispatch_id="d-010")
    store.append("T2", {"type": "result", "data": {}}, dispatch_id="d-010")
    store.clear("T2", archive_dispatch_id="d-010")

    monkeypatch.setattr(mod, "_store", store)

    handler = _make_handler()
    mod.handle_agent_stream_archive(handler, "T2", "d-010")

    body = handler.wfile.getvalue()
    result = json.loads(body)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert result[1]["type"] == "result"


def test_archive_404_on_missing(tmp_path, monkeypatch):
    import api_agent_stream as mod
    from event_store import EventStore

    store = EventStore(events_dir=tmp_path / "events")
    monkeypatch.setattr(mod, "_store", store)

    handler = _make_handler()
    mod.handle_agent_stream_archive(handler, "T1", "nonexistent-dispatch")

    handler.send_response.assert_called_once_with(HTTPStatus.NOT_FOUND)
