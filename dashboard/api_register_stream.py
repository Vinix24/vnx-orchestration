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


def _resolve_start_index(path: Path, since_ts: str | None) -> int:
    """Convert a since_ts API parameter into a line-index cursor.

    Returns the count of records (non-blank lines) whose timestamp is
    less than or equal to since_ts. Records past this point are the
    "new" events the caller wants delivered.

    Records with no timestamp or with malformed JSON are treated as
    already-seen (their slot is consumed by the cursor) so that the
    line index stays in sync with file position regardless of content.

    A None since_ts means "start from the beginning" → 0.
    """
    if not since_ts or not path.exists():
        return 0
    index = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    index += 1
                    continue
                ts = rec.get("timestamp", "")
                if ts and ts > since_ts:
                    break
                index += 1
    except OSError:
        return 0
    return index


def _read_new_events_after(
    path: Path,
    last_index: int,
    event_types: set[str] | None,
) -> tuple[list[dict], int]:
    """Read records past line-index ``last_index``.

    Returns ``(events, new_last_index)`` where ``new_last_index`` is the
    record-slot count after the last consumed line (whether delivered,
    filtered out, or malformed). Using a positional cursor instead of
    timestamps means two events written with the same timestamp are
    both delivered — the timestamp-only cursor previously dropped
    same-ts records (codex regate finding, PR #304).
    """
    if not path.exists():
        return [], last_index
    events: list[dict] = []
    new_index = last_index
    current = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                current += 1
                if current <= last_index:
                    continue
                # This slot is being consumed — advance the cursor even
                # when the record is malformed or filtered out, so we
                # never re-scan it on the next poll.
                new_index = current
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    print(
                        f"[register-stream] WARNING: malformed JSON line skipped: {stripped[:80]}",
                        file=sys.stderr,
                    )
                    continue
                if event_types and rec.get("event") not in event_types:
                    continue
                events.append(rec)
    except OSError:
        return [], last_index
    return events, new_index


def _read_new_events(
    path: Path,
    since_ts: str | None,
    event_types: set[str] | None,
) -> tuple[list[dict], str | None]:
    """Backward-compatible wrapper around the line-indexed reader.

    Returns ``(events, latest_timestamp)`` where ``latest_timestamp`` is
    the maximum timestamp seen in the returned events (or ``since_ts``
    if none). Internally uses the line-index cursor to avoid the
    same-timestamp-skip bug, but presents a timestamp-shaped result
    for callers that have not migrated.
    """
    start_index = _resolve_start_index(path, since_ts)
    events, _new_index = _read_new_events_after(path, start_index, event_types)
    latest_ts = since_ts
    for rec in events:
        ts = rec.get("timestamp", "")
        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
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

    The cursor is a line index (count of records already delivered) so
    that two events sharing a timestamp are both streamed — the previous
    timestamp-only cursor silently dropped the second one.

    Query params (handled by caller, passed as args here):
      since_ts    ISO8601 timestamp — replay only events strictly newer than this.
                  Resolved once at session start to a line-index cursor.
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

    last_index = _resolve_start_index(src, since_ts)
    last_heartbeat = time.monotonic()

    try:
        while True:
            events, last_index = _read_new_events_after(src, last_index, event_types)
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
