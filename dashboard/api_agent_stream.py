"""Agent stream SSE endpoint handlers.

Provides Server-Sent Events streaming from EventStore NDJSON files
and a status endpoint listing terminals with available events.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem only.
"""

from __future__ import annotations

import json
import os
import re
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

# Lane ids are arbitrary slugs (terminals T0-T3, provider lanes, tmux-spawn
# dispatch ids). The single source of truth for which lanes exist is the events
# directory itself — see EventStore.list_lanes(). The only hard constraint is
# path-traversal safety: a lane id must be a flat slug (no dots, slashes, "..").
_LANE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _valid_lane(lane: str) -> bool:
    """Path-traversal-safe lane id check (replaces the old fixed T0-T3 set)."""
    return bool(_LANE_RE.match(lane))


_POLL_INTERVAL = 0.5  # seconds between polls for new events


def handle_agent_stream(handler: BaseHTTPRequestHandler, terminal: str, since: str | None) -> None:
    """Stream events for a lane as SSE.

    Keeps the connection open, polling every 500ms for new events.
    Stops when the client disconnects (BrokenPipeError / ConnectionResetError).
    """
    if not _valid_lane(terminal):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": f"Invalid lane: {terminal}"})
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
    """Return JSON listing lanes that have agent events.

    Lanes are discovered from the events directory (EventStore.list_lanes),
    so the dashboard renders whatever actually produced events — terminals
    T0-T3, provider lanes, and tmux-spawn dispatch ids — instead of a fixed
    T0-T3 set. Domain/system streams (decisions, provider_costs, ...) are
    filtered out by the agent-envelope check.

    The response keeps the legacy ``terminals`` key (a map of lane id ->
    {event_count, last_timestamp, provider, dispatch_id}) for back-compat, and
    adds ``lanes`` as an ordered list (most-recently-active first) for clients
    that want deterministic ordering.
    """
    lanes = _store.list_lanes()
    terminals = {
        lane["id"]: {
            "event_count": lane["event_count"],
            "last_timestamp": lane["last_timestamp"],
            "provider": lane.get("provider"),
            "dispatch_id": lane.get("dispatch_id"),
        }
        for lane in lanes
    }
    _send_json(handler, HTTPStatus.OK, {"terminals": terminals, "lanes": lanes})


def handle_agent_stream_archive_list(handler: "BaseHTTPRequestHandler", terminal: str) -> None:
    """List all archived dispatch IDs for a lane."""
    if not _valid_lane(terminal):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": f"Invalid lane: {terminal}"})
        return

    archive_dir = _store.archive_dir(terminal)
    if not archive_dir.exists():
        _send_json(handler, HTTPStatus.OK, [])
        return

    entries = []
    for f in sorted(archive_dir.glob("*.ndjson")):
        stat = f.stat()
        entries.append({
            "dispatch_id": f.stem,
            "file_size": stat.st_size,
            "modified_at": stat.st_mtime,
        })
    _send_json(handler, HTTPStatus.OK, entries)


def handle_agent_stream_archive(
    handler: "BaseHTTPRequestHandler", terminal: str, dispatch_id: str
) -> None:
    """Return all events for a specific archived dispatch."""
    if not _valid_lane(terminal):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": f"Invalid lane: {terminal}"})
        return

    if not _LANE_RE.match(dispatch_id):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "Invalid dispatch_id"})
        return

    archive_file = _store.archive_dir(terminal) / f"{dispatch_id}.ndjson"
    if not archive_file.exists():
        _send_json(handler, HTTPStatus.NOT_FOUND, {"error": f"Archive not found: {dispatch_id}"})
        return

    events = []
    with open(archive_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    _send_json(handler, HTTPStatus.OK, events)


def _send_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)
