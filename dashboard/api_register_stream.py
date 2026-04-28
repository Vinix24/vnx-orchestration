"""Register stream SSE endpoint handlers.

Provides Server-Sent Events streaming from dispatch_register.ndjson
and a one-shot archive replay endpoint.

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

# Make scripts/lib importable for path resolution
_SCRIPTS_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)

from project_root import resolve_state_dir

_POLL_INTERVAL = 0.5  # seconds between polls
_HEARTBEAT_INTERVAL = float(os.environ.get("VNX_REGISTER_STREAM_HEARTBEAT", "30"))


def _register_file() -> Path:
    return resolve_state_dir(__file__) / "dispatch_register.ndjson"


def _read_new_events(
    path: Path,
    since_ts: str | None,
    event_types: set[str] | None,
) -> tuple[list[dict], str | None]:
    """Read events from path that are strictly newer than since_ts.

    Returns (events, latest_timestamp) where latest_timestamp is the
    maximum timestamp seen in the returned events (or since_ts if none).
    Skips malformed JSON lines with a stderr warning.
    """
    if not path.exists():
        return [], since_ts
    events = []
    latest_ts = since_ts
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"[register-stream] WARNING: malformed JSON line skipped: {line[:80]}",
                        file=sys.stderr,
                    )
                    continue
                ts = rec.get("timestamp", "")
                if since_ts and ts <= since_ts:
                    continue
                if event_types and rec.get("event") not in event_types:
                    continue
                events.append(rec)
                if ts and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts
    except OSError:
        return [], since_ts
    return events, latest_ts


def handle_register_stream(
    handler: "BaseHTTPRequestHandler",
    since_ts: str | None = None,
    event_type_filter: str | None = None,
    *,
    poll_interval: float = _POLL_INTERVAL,
    heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    register_file: Path | None = None,
) -> None:
    """Stream dispatch_register.ndjson as SSE.

    Keeps the connection open, polling every 500ms for new events.
    Sends a heartbeat comment every heartbeat_interval seconds to keep the
    connection alive. Stops when the client disconnects.

    Query params (handled by caller, passed as args here):
      since_ts    ISO8601 timestamp — replay only events strictly newer than this
      event_type  comma-separated event type filter (e.g. dispatch_created,gate_passed)
    """
    src = register_file if register_file is not None else _register_file()

    event_types: set[str] | None = None
    if event_type_filter:
        event_types = {e.strip() for e in event_type_filter.split(",") if e.strip()}

    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    last_ts = since_ts
    last_heartbeat = time.monotonic()

    try:
        while True:
            events, last_ts = _read_new_events(src, last_ts, event_types)
            for event in events:
                line = f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                handler.wfile.write(line.encode("utf-8"))

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                handler.wfile.write(b": heartbeat\n\n")
                last_heartbeat = now

            handler.wfile.flush()
            time.sleep(poll_interval)
    except (BrokenPipeError, ConnectionResetError, OSError):
        # Client disconnected — clean exit
        pass


def handle_register_stream_archive(
    handler: "BaseHTTPRequestHandler",
    register_file: Path | None = None,
) -> None:
    """Return full dispatch_register.ndjson as a JSON array (one-shot, not SSE).

    Malformed lines are skipped with a stderr warning. Missing file returns [].
    """
    src = register_file if register_file is not None else _register_file()
    events: list[dict] = []
    if src.exists():
        try:
            with open(src, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        print(
                            f"[register-stream] WARNING: malformed JSON line skipped in archive: {line[:80]}",
                            file=sys.stderr,
                        )
        except OSError:
            pass
    _send_json(handler, HTTPStatus.OK, events)


def _send_json(handler: "BaseHTTPRequestHandler", status: HTTPStatus, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)
