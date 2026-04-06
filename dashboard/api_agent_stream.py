"""Agent stream SSE endpoint handlers.

Provides Server-Sent Events streaming from EventStore NDJSON files
and a status endpoint listing terminals with available events.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

# Make scripts/lib importable for EventStore
_SCRIPTS_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)

from event_store import EventStore

_store = EventStore()

VALID_TERMINALS = frozenset({"T0", "T1", "T2", "T3"})

_POLL_INTERVAL = 0.5  # seconds between polls for new events


def handle_agent_stream(handler: BaseHTTPRequestHandler, terminal: str, since: str | None) -> None:
    """Stream events for a terminal as SSE.

    Keeps the connection open, polling every 500ms for new events.
    Stops when the client disconnects (BrokenPipeError / ConnectionResetError).
    """
    if terminal not in VALID_TERMINALS:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": f"Invalid terminal: {terminal}"})
        return

    if _store.event_count(terminal) == 0:
        _send_json(handler, HTTPStatus.NOT_FOUND, {"error": f"No events for terminal {terminal}"})
        return

    # Send SSE headers
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    last_timestamp = since

    try:
        while True:
            events = list(_store.tail(terminal, since=last_timestamp))
            for event in events:
                line = f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                handler.wfile.write(line.encode("utf-8"))
                ts = event.get("timestamp")
                if ts:
                    last_timestamp = ts

            handler.wfile.flush()
            time.sleep(_POLL_INTERVAL)
    except (BrokenPipeError, ConnectionResetError, OSError):
        # Client disconnected — clean exit
        pass


def handle_agent_stream_status(handler: BaseHTTPRequestHandler) -> None:
    """Return JSON listing terminals with event data."""
    terminals = {}
    for tid in sorted(VALID_TERMINALS):
        count = _store.event_count(tid)
        if count > 0:
            last = _store.last_event(tid)
            terminals[tid] = {
                "event_count": count,
                "last_timestamp": last.get("timestamp") if last else None,
            }

    _send_json(handler, HTTPStatus.OK, {"terminals": terminals})


def _send_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)
