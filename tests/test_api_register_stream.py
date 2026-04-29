#!/usr/bin/env python3
"""Tests for /api/register-stream SSE endpoint and /api/register-stream/archive.

Covers:
  A. Append event to test file → SSE client receives event in first poll
  B. since_ts filter — only events strictly after timestamp are replayed
  C. event_type filter — only matching events streamed
  D. Heartbeat sent when interval elapses (using heartbeat_interval=0)
  E. Archive endpoint returns full content as JSON array
  F. Malformed JSON line in source file → SSE skips with stderr warning, continues
"""

from __future__ import annotations

import io
import json
import sys
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add dashboard and scripts/lib to path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

from api_register_stream import (
    handle_register_stream,
    handle_register_stream_archive,
    _read_new_events,
    _read_new_events_after,
    _resolve_start_index,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, separators=(",", ":")) + "\n")


def _make_event(event: str, ts: str, **kwargs) -> dict:
    rec = {"timestamp": ts, "event": event, "dispatch_id": "test-001"}
    rec.update(kwargs)
    return rec


def _run_one_poll(handler, reg_file: Path, since_ts=None, event_type_filter=None):
    """Run one iteration of handle_register_stream then disconnect."""
    flush_count = 0

    def limited_flush():
        nonlocal flush_count
        flush_count += 1
        if flush_count >= 1:
            raise BrokenPipeError("test disconnect")

    handler.wfile.flush = limited_flush
    handle_register_stream(
        handler,
        since_ts=since_ts,
        event_type_filter=event_type_filter,
        poll_interval=0,
        heartbeat_interval=9999,  # suppress heartbeat unless explicitly testing it
        register_file=reg_file,
    )


def _sse_lines(handler) -> list[dict]:
    output = handler.wfile.getvalue().decode("utf-8")
    result = []
    for line in output.split("\n"):
        if line.startswith("data: "):
            result.append(json.loads(line[len("data: "):]))
    return result


# ---------------------------------------------------------------------------
# Case A: SSE client receives event on first poll
# ---------------------------------------------------------------------------

class TestCaseA_EventReceived:
    def test_single_event_appears_in_sse_output(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        ev = _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z")
        _write_events(reg, [ev])

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        assert len(lines) == 1
        assert lines[0]["event"] == "dispatch_created"

    def test_multiple_events_all_received(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_started", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        assert len(lines) == 3
        assert [l["event"] for l in lines] == [
            "dispatch_created", "dispatch_promoted", "dispatch_started"
        ]

    def test_sse_headers_sent(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        _write_events(reg, [_make_event("dispatch_created", "2026-04-28T10:00:00.000000Z")])

        handler = _make_handler()
        _run_one_poll(handler, reg)

        handler.send_response.assert_called_with(HTTPStatus.OK)
        handler.send_header.assert_any_call("Content-Type", "text/event-stream")
        handler.send_header.assert_any_call("Cache-Control", "no-cache")
        handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")

    def test_empty_file_produces_no_sse_data(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        assert lines == []

    def test_missing_file_produces_no_sse_data(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"  # does not exist

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        assert lines == []


# ---------------------------------------------------------------------------
# Case B: since_ts filter
# ---------------------------------------------------------------------------

class TestCaseB_SinceTsFilter:
    def test_events_before_since_ts_excluded(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_started", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        # Pass timestamp of the first event — only subsequent events should appear
        _run_one_poll(handler, reg, since_ts="2026-04-28T10:00:00.000000Z")

        lines = _sse_lines(handler)
        assert len(lines) == 2
        assert lines[0]["event"] == "dispatch_promoted"
        assert lines[1]["event"] == "dispatch_started"

    def test_all_events_excluded_when_since_ts_is_latest(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, since_ts="2026-04-28T10:00:01.000000Z")

        lines = _sse_lines(handler)
        assert lines == []

    def test_no_since_ts_returns_all_events(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, since_ts=None)

        lines = _sse_lines(handler)
        assert len(lines) == 2

    def test_read_new_events_tracks_latest_ts(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        events, latest_ts = _read_new_events(reg, None, None)
        assert len(events) == 2
        assert latest_ts == "2026-04-28T10:00:02.000000Z"

    def test_read_new_events_advances_since_ts_across_polls(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
        ]
        _write_events(reg, evs)

        # First poll: get all events
        events, ts1 = _read_new_events(reg, None, None)
        assert len(events) == 2

        # Append a new event
        _write_events(reg, [_make_event("dispatch_started", "2026-04-28T10:00:02.000000Z")])

        # Second poll: only the new event
        events2, ts2 = _read_new_events(reg, ts1, None)
        assert len(events2) == 1
        assert events2[0]["event"] == "dispatch_started"
        assert ts2 == "2026-04-28T10:00:02.000000Z"


# ---------------------------------------------------------------------------
# Case C: event_type filter
# ---------------------------------------------------------------------------

class TestCaseC_EventTypeFilter:
    def test_single_type_filter(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("gate_passed", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_completed", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, event_type_filter="gate_passed")

        lines = _sse_lines(handler)
        assert len(lines) == 1
        assert lines[0]["event"] == "gate_passed"

    def test_multi_type_filter_comma_separated(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("gate_passed", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_completed", "2026-04-28T10:00:02.000000Z"),
            _make_event("gate_failed", "2026-04-28T10:00:03.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, event_type_filter="gate_passed,gate_failed")

        lines = _sse_lines(handler)
        assert len(lines) == 2
        types = [l["event"] for l in lines]
        assert "gate_passed" in types
        assert "gate_failed" in types

    def test_filter_no_match_returns_nothing(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, event_type_filter="pr_opened")

        lines = _sse_lines(handler)
        assert lines == []

    def test_filter_with_whitespace_in_comma_list(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("gate_passed", "2026-04-28T10:00:01.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, event_type_filter=" gate_passed , dispatch_created ")

        lines = _sse_lines(handler)
        assert len(lines) == 2

    def test_no_filter_returns_all_types(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("gate_passed", "2026-04-28T10:00:01.000000Z"),
            _make_event("pr_merged", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        _run_one_poll(handler, reg, event_type_filter=None)

        lines = _sse_lines(handler)
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Case D: heartbeat
# ---------------------------------------------------------------------------

class TestCaseD_Heartbeat:
    def test_heartbeat_sent_when_interval_zero(self, tmp_path):
        """heartbeat_interval=0 means heartbeat fires on every loop iteration."""
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")  # empty file

        handler = _make_handler()
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count >= 1:
                raise BrokenPipeError("test disconnect")

        handler.wfile.flush = limited_flush
        handle_register_stream(
            handler,
            since_ts=None,
            event_type_filter=None,
            poll_interval=0,
            heartbeat_interval=0,
            register_file=reg,
        )

        output = handler.wfile.getvalue().decode("utf-8")
        assert ": heartbeat\n\n" in output

    def test_no_heartbeat_when_interval_not_elapsed(self, tmp_path):
        """With large heartbeat_interval, no heartbeat in a single poll."""
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")

        handler = _make_handler()
        _run_one_poll(handler, reg)  # heartbeat_interval=9999

        output = handler.wfile.getvalue().decode("utf-8")
        assert ": heartbeat" not in output

    def test_heartbeat_line_format(self, tmp_path):
        """Heartbeat must be SSE comment format: ': heartbeat\n\n'."""
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")

        handler = _make_handler()
        flush_count = 0

        def limited_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count >= 1:
                raise BrokenPipeError()

        handler.wfile.flush = limited_flush
        handle_register_stream(
            handler,
            poll_interval=0,
            heartbeat_interval=0,
            register_file=reg,
        )

        output = handler.wfile.getvalue().decode("utf-8")
        assert ": heartbeat\n\n" in output


# ---------------------------------------------------------------------------
# Case E: archive endpoint
# ---------------------------------------------------------------------------

class TestCaseE_Archive:
    def test_archive_returns_all_events_as_array(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_completed", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        handler = _make_handler()
        handle_register_stream_archive(handler, register_file=reg)

        handler.send_response.assert_called_with(HTTPStatus.OK)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert isinstance(body, list)
        assert len(body) == 3
        assert body[0]["event"] == "dispatch_created"
        assert body[2]["event"] == "dispatch_completed"

    def test_archive_returns_empty_array_for_missing_file(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"  # does not exist

        handler = _make_handler()
        handle_register_stream_archive(handler, register_file=reg)

        handler.send_response.assert_called_with(HTTPStatus.OK)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert body == []

    def test_archive_returns_empty_array_for_empty_file(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")

        handler = _make_handler()
        handle_register_stream_archive(handler, register_file=reg)

        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert body == []

    def test_archive_content_type_is_json(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")

        handler = _make_handler()
        handle_register_stream_archive(handler, register_file=reg)

        handler.send_header.assert_any_call("Content-Type", "application/json")
        handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")

    def test_archive_skips_malformed_lines(self, tmp_path, capsys):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write('{"timestamp":"2026-04-28T10:00:00.000000Z","event":"dispatch_created","dispatch_id":"x"}\n')
            fh.write("INVALID JSON LINE\n")
            fh.write('{"timestamp":"2026-04-28T10:00:01.000000Z","event":"dispatch_promoted","dispatch_id":"x"}\n')

        handler = _make_handler()
        handle_register_stream_archive(handler, register_file=reg)

        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert len(body) == 2
        assert body[0]["event"] == "dispatch_created"
        assert body[1]["event"] == "dispatch_promoted"

        captured = capsys.readouterr()
        assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# Case F: malformed JSON in SSE stream
# ---------------------------------------------------------------------------

class TestCaseF_MalformedJsonInStream:
    def test_sse_skips_malformed_line_and_continues(self, tmp_path, capsys):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write('{"timestamp":"2026-04-28T10:00:00.000000Z","event":"dispatch_created","dispatch_id":"x"}\n')
            fh.write("NOT VALID JSON\n")
            fh.write("{also not valid\n")
            fh.write('{"timestamp":"2026-04-28T10:00:01.000000Z","event":"dispatch_completed","dispatch_id":"x"}\n')

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        # Valid events are streamed; malformed lines skipped
        assert len(lines) == 2
        assert lines[0]["event"] == "dispatch_created"
        assert lines[1]["event"] == "dispatch_completed"

        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_all_malformed_lines_produce_no_sse_output(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("BAD LINE 1\n")
            fh.write("BAD LINE 2\n")

        handler = _make_handler()
        _run_one_poll(handler, reg)

        lines = _sse_lines(handler)
        assert lines == []

    def test_client_disconnect_stops_cleanly(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        _write_events(reg, [_make_event("dispatch_created", "2026-04-28T10:00:00.000000Z")])

        handler = _make_handler()

        def raise_on_write(data):
            raise BrokenPipeError("client gone")

        handler.wfile.write = raise_on_write

        # Should not raise — disconnect handled gracefully
        handle_register_stream(
            handler,
            poll_interval=0,
            heartbeat_interval=9999,
            register_file=reg,
        )

    def test_connection_reset_stops_cleanly(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        _write_events(reg, [_make_event("dispatch_created", "2026-04-28T10:00:00.000000Z")])

        handler = _make_handler()

        def raise_on_write(data):
            raise ConnectionResetError("reset")

        handler.wfile.write = raise_on_write

        handle_register_stream(
            handler,
            poll_interval=0,
            heartbeat_interval=9999,
            register_file=reg,
        )


# ---------------------------------------------------------------------------
# Case G: same-timestamp regression (codex regate finding for PR #304)
#
# A timestamp-only cursor silently dropped a second event whose timestamp
# equalled the last-delivered event's timestamp. The line-index cursor
# must deliver both records.
# ---------------------------------------------------------------------------

class TestCaseG_SameTimestampRegression:
    def test_read_new_events_after_delivers_same_ts_appends(self, tmp_path):
        """Two records sharing a timestamp must both be returned across polls."""
        reg = tmp_path / "dispatch_register.ndjson"
        same_ts = "2026-04-28T10:00:00.000000Z"
        _write_events(reg, [_make_event("dispatch_created", same_ts)])

        # First poll consumes the only record.
        events1, idx1 = _read_new_events_after(reg, 0, None)
        assert len(events1) == 1
        assert events1[0]["event"] == "dispatch_created"
        assert idx1 == 1

        # Append a second record with the IDENTICAL timestamp.
        _write_events(reg, [_make_event("dispatch_promoted", same_ts)])

        # Second poll must return the new record despite ts collision.
        events2, idx2 = _read_new_events_after(reg, idx1, None)
        assert len(events2) == 1
        assert events2[0]["event"] == "dispatch_promoted"
        assert idx2 == 2

    def test_read_new_events_wrapper_first_read_keeps_same_ts(self, tmp_path):
        """The legacy wrapper's timestamp output cannot disambiguate same-ts
        records — but the wrapper still uses the line-index reader internally,
        so the FIRST poll returns both same-ts events together."""
        reg = tmp_path / "dispatch_register.ndjson"
        same_ts = "2026-04-28T10:00:00.000000Z"
        _write_events(reg, [
            _make_event("dispatch_created", same_ts),
            _make_event("dispatch_promoted", same_ts),
        ])

        events, latest_ts = _read_new_events(reg, None, None)
        assert len(events) == 2
        assert latest_ts == same_ts

    def test_handle_register_stream_delivers_same_ts_across_polls(self, tmp_path):
        """End-to-end: streaming handler must deliver back-to-back same-ts
        appends across consecutive polls (the codex repro)."""
        reg = tmp_path / "dispatch_register.ndjson"
        same_ts = "2026-04-28T10:00:00.000000Z"
        _write_events(reg, [_make_event("dispatch_created", same_ts)])

        handler = _make_handler()

        # Run two polls: append between the first and second flush, then
        # raise BrokenPipe to stop. The cursor inside the handler must
        # advance by line index, not timestamp, so the second event is
        # streamed despite sharing the first event's timestamp.
        flush_count = 0

        def staged_flush():
            nonlocal flush_count
            flush_count += 1
            if flush_count == 1:
                _write_events(reg, [_make_event("dispatch_promoted", same_ts)])
                return
            raise BrokenPipeError("test disconnect")

        handler.wfile.flush = staged_flush
        handle_register_stream(
            handler,
            since_ts=None,
            event_type_filter=None,
            poll_interval=0,
            heartbeat_interval=9999,
            register_file=reg,
        )

        lines = _sse_lines(handler)
        assert len(lines) == 2
        assert [l["event"] for l in lines] == ["dispatch_created", "dispatch_promoted"]

    def test_resolve_start_index_basic(self, tmp_path):
        reg = tmp_path / "dispatch_register.ndjson"
        evs = [
            _make_event("dispatch_created", "2026-04-28T10:00:00.000000Z"),
            _make_event("dispatch_promoted", "2026-04-28T10:00:01.000000Z"),
            _make_event("dispatch_started", "2026-04-28T10:00:02.000000Z"),
        ]
        _write_events(reg, evs)

        # No since_ts → start at 0
        assert _resolve_start_index(reg, None) == 0

        # since_ts at first record → skip just the first
        assert _resolve_start_index(reg, "2026-04-28T10:00:00.000000Z") == 1

        # since_ts past everything → skip all
        assert _resolve_start_index(reg, "2026-04-28T10:00:09.000000Z") == 3

        # Missing file → 0
        missing = tmp_path / "missing.ndjson"
        assert _resolve_start_index(missing, "2026-04-28T10:00:00.000000Z") == 0

    def test_resolve_start_index_groups_same_ts(self, tmp_path):
        """If multiple records share a timestamp equal to since_ts, all of
        them are treated as already seen (cursor advances past all of them).
        Records strictly newer than since_ts are returned to the caller."""
        reg = tmp_path / "dispatch_register.ndjson"
        same_ts = "2026-04-28T10:00:00.000000Z"
        next_ts = "2026-04-28T10:00:01.000000Z"
        evs = [
            _make_event("e1", same_ts),
            _make_event("e2", same_ts),
            _make_event("e3", next_ts),
        ]
        _write_events(reg, evs)

        idx = _resolve_start_index(reg, same_ts)
        assert idx == 2

        events, new_idx = _read_new_events_after(reg, idx, None)
        assert len(events) == 1
        assert events[0]["event"] == "e3"
        assert new_idx == 3

    def test_read_new_events_after_advances_cursor_past_filtered(self, tmp_path):
        """Filtered-out and malformed records still advance the cursor so
        they aren't re-scanned on the next poll."""
        reg = tmp_path / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True, exist_ok=True)
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_make_event("dispatch_created", "2026-04-28T10:00:00.000000Z")) + "\n")
            fh.write("not-json\n")
            fh.write(json.dumps(_make_event("gate_passed", "2026-04-28T10:00:01.000000Z")) + "\n")

        events, idx = _read_new_events_after(reg, 0, {"gate_passed"})
        # Only gate_passed matches the filter, but the cursor advanced
        # past all three slots (created, malformed, gate_passed).
        assert len(events) == 1
        assert events[0]["event"] == "gate_passed"
        assert idx == 3

        # Second poll on unchanged file returns nothing and keeps cursor.
        events2, idx2 = _read_new_events_after(reg, idx, {"gate_passed"})
        assert events2 == []
        assert idx2 == 3
