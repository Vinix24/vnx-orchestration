#!/usr/bin/env python3
"""Tests for t0_decision_log.py — passive decision writer.

Covers:
- build_record: schema conformance and defaults
- record_from_executor_event: all event types + unknown type + missing fields
- write_decision: JSONL append with file locking, directory creation
- load_cursor / save_cursor: round-trip, missing file, corrupt file
- process_events_file: cursor advancement, idempotency, dry-run, empty/missing file
- main() CLI: dry-run, missing events file (no-op, exit 0)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from t0_decision_log import (
    DEFAULT_CURSOR_FILE,
    DEFAULT_DECISION_LOG,
    DEFAULT_EVENTS_FILE,
    _EVENT_TYPE_TO_ACTION,
    _TERMINAL_TO_TRACK,
    build_record,
    load_cursor,
    main,
    process_events_file,
    record_from_executor_event,
    save_cursor,
    write_decision,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

DISPATCH_EVENT = {
    "event_type": "t0_dispatch",
    "dispatch_id": "20260422-103500-f36-A",
    "dispatch_target": "T1",
    "trigger_reason": "new_report",
    "task_hash": "abc123",
    "timestamp": "2026-04-22T10:35:00+00:00",
}

WAIT_EVENT = {
    "event_type": "t0_wait",
    "reason": "Waiting for Track B receipt",
    "timestamp": "2026-04-22T10:36:00+00:00",
}

COMPLETE_EVENT = {
    "event_type": "t0_complete",
    "reason": "Feature closure verified",
    "timestamp": "2026-04-22T10:37:00+00:00",
}

REJECT_EVENT = {
    "event_type": "t0_reject",
    "reason": "Tests failed in CI",
    "timestamp": "2026-04-22T10:38:00+00:00",
}

ESCALATE_EVENT = {
    "event_type": "t0_escalate",
    "reason": "Blocker: merge conflict unresolvable",
    "timestamp": "2026-04-22T10:39:00+00:00",
}

UNKNOWN_DECISION_EVENT = {
    "event_type": "t0_unknown_decision",
    "raw_decision": {"decision": "FOOBAR"},
    "timestamp": "2026-04-22T10:40:00+00:00",
}

ALL_EVENTS = [
    DISPATCH_EVENT,
    WAIT_EVENT,
    COMPLETE_EVENT,
    REJECT_EVENT,
    ESCALATE_EVENT,
    UNKNOWN_DECISION_EVENT,
]


def make_events_file(tmp_path: Path, events: list[dict]) -> Path:
    f = tmp_path / "t0_decisions.ndjson"
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return f


# ---------------------------------------------------------------------------
# build_record
# ---------------------------------------------------------------------------

class TestBuildRecord:
    def test_required_schema_keys_present(self):
        r = build_record("dispatch")
        for key in ("timestamp", "session_summary_at", "action", "dispatch_id",
                     "track", "reasoning", "open_items_actions", "next_expected"):
            assert key in r, f"Missing key: {key}"

    def test_action_preserved(self):
        for action in ("dispatch", "approve", "reject", "escalate", "wait", "close_oi", "advance_gate"):
            r = build_record(action)
            assert r["action"] == action

    def test_defaults_for_optional_fields(self):
        r = build_record("wait")
        assert r["dispatch_id"] is None
        assert r["track"] is None
        assert r["reasoning"] == ""
        assert r["open_items_actions"] == []
        assert r["next_expected"] == ""

    def test_custom_timestamp_used(self):
        r = build_record("wait", timestamp="2026-01-01T00:00:00+00:00")
        assert r["timestamp"] == "2026-01-01T00:00:00+00:00"
        assert r["session_summary_at"] == "2026-01-01T00:00:00+00:00"

    def test_open_items_actions_passed_through(self):
        oia = [{"action": "close", "id": "OI-001", "reason": "done"}]
        r = build_record("close_oi", open_items_actions=oia)
        assert r["open_items_actions"] == oia

    def test_timestamp_generated_when_absent(self):
        r = build_record("wait")
        assert "T" in r["timestamp"]  # ISO-8601 contains T


# ---------------------------------------------------------------------------
# record_from_executor_event
# ---------------------------------------------------------------------------

class TestRecordFromExecutorEvent:
    def test_dispatch_event_action(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert r is not None
        assert r["action"] == "dispatch"

    def test_dispatch_event_track_mapped(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert r["track"] == "A"  # T1 → A

    def test_dispatch_event_dispatch_id_preserved(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert r["dispatch_id"] == "20260422-103500-f36-A"

    def test_dispatch_event_reasoning_contains_target(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert "T1" in r["reasoning"]

    def test_dispatch_event_reasoning_contains_trigger_reason(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert "new_report" in r["reasoning"]

    def test_dispatch_event_next_expected_contains_target(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert "T1" in r["next_expected"]

    def test_wait_event_action(self):
        r = record_from_executor_event(WAIT_EVENT)
        assert r is not None
        assert r["action"] == "wait"

    def test_wait_event_reasoning(self):
        r = record_from_executor_event(WAIT_EVENT)
        assert "Waiting for Track B receipt" in r["reasoning"]

    def test_complete_event_action(self):
        r = record_from_executor_event(COMPLETE_EVENT)
        assert r is not None
        assert r["action"] == "close_oi"

    def test_reject_event_action(self):
        r = record_from_executor_event(REJECT_EVENT)
        assert r is not None
        assert r["action"] == "reject"

    def test_reject_event_reasoning(self):
        r = record_from_executor_event(REJECT_EVENT)
        assert "Tests failed" in r["reasoning"]

    def test_escalate_event_action(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r is not None
        assert r["action"] == "escalate"

    def test_escalate_event_next_expected(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert "Operator" in r["next_expected"]

    def test_unknown_decision_event_action(self):
        r = record_from_executor_event(UNKNOWN_DECISION_EVENT)
        assert r is not None
        assert r["action"] == "wait"

    def test_unknown_decision_reasoning_includes_type(self):
        r = record_from_executor_event(UNKNOWN_DECISION_EVENT)
        assert "FOOBAR" in r["reasoning"]

    def test_unrecognised_event_type_returns_none(self):
        r = record_from_executor_event({"event_type": "some_other_thing"})
        assert r is None

    def test_missing_event_type_returns_none(self):
        r = record_from_executor_event({})
        assert r is None

    def test_timestamp_preserved_from_event(self):
        r = record_from_executor_event(DISPATCH_EVENT)
        assert r["timestamp"] == "2026-04-22T10:35:00+00:00"

    def test_dispatch_missing_target_track_is_none(self):
        event = {**DISPATCH_EVENT, "dispatch_target": "T99"}
        r = record_from_executor_event(event)
        assert r["track"] is None

    def test_dispatch_empty_trigger_reason(self):
        event = {**DISPATCH_EVENT, "trigger_reason": ""}
        r = record_from_executor_event(event)
        assert "—" not in r["reasoning"]  # no em-dash when no trigger reason

    def test_escalate_missing_reason_uses_default(self):
        event = {"event_type": "t0_escalate", "timestamp": "2026-04-22T10:00:00+00:00"}
        r = record_from_executor_event(event)
        assert r["reasoning"] == "Escalation triggered"


# ---------------------------------------------------------------------------
# write_decision
# ---------------------------------------------------------------------------

class TestWriteDecision:
    def test_writes_valid_jsonl_line(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        record = build_record("dispatch", reasoning="ok", dispatch_id="abc")
        write_decision(record, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "dispatch"
        assert parsed["dispatch_id"] == "abc"

    def test_appends_multiple_records(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        for action in ("dispatch", "wait", "reject"):
            write_decision(build_record(action), log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[2])["action"] == "reject"

    def test_creates_parent_directory(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "log.jsonl"
        write_decision(build_record("wait"), log_file)
        assert log_file.exists()

    def test_appends_to_existing_file(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        log_file.write_text('{"action":"prior"}\n')
        write_decision(build_record("wait"), log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["action"] == "prior"
        assert json.loads(lines[1])["action"] == "wait"

    def test_each_line_is_valid_json(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        for i in range(5):
            write_decision(build_record("wait", reasoning=str(i)), log_file)
        for line in log_file.read_text().strip().splitlines():
            json.loads(line)  # must not raise


# ---------------------------------------------------------------------------
# load_cursor / save_cursor
# ---------------------------------------------------------------------------

class TestCursor:
    def test_load_cursor_returns_zero_when_missing(self, tmp_path):
        assert load_cursor(tmp_path / "cursor.json") == 0

    def test_save_and_load_round_trip(self, tmp_path):
        cursor_file = tmp_path / "cursor.json"
        save_cursor(cursor_file, 42)
        assert load_cursor(cursor_file) == 42

    def test_load_cursor_returns_zero_on_corrupt_file(self, tmp_path):
        cursor_file = tmp_path / "cursor.json"
        cursor_file.write_text("NOT_JSON")
        assert load_cursor(cursor_file) == 0

    def test_save_cursor_creates_parent_directory(self, tmp_path):
        cursor_file = tmp_path / "state" / "cursor.json"
        save_cursor(cursor_file, 7)
        assert cursor_file.exists()
        assert load_cursor(cursor_file) == 7

    def test_save_cursor_overwrites_existing(self, tmp_path):
        cursor_file = tmp_path / "cursor.json"
        save_cursor(cursor_file, 5)
        save_cursor(cursor_file, 10)
        assert load_cursor(cursor_file) == 10


# ---------------------------------------------------------------------------
# process_events_file
# ---------------------------------------------------------------------------

class TestProcessEventsFile:
    def test_writes_all_convertible_events(self, tmp_path):
        events_file = make_events_file(tmp_path, ALL_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        # All 6 event types produce records
        assert written == 6
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 6

    def test_cursor_advanced_after_processing(self, tmp_path):
        events_file = make_events_file(tmp_path, ALL_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        # File has 6 events + trailing newline (which produces 7 raw lines including empty)
        # Cursor should be at end of file lines count
        cursor = load_cursor(cursor_file)
        assert cursor > 0

    def test_idempotent_on_second_run(self, tmp_path):
        events_file = make_events_file(tmp_path, ALL_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        written_second = process_events_file(events_file, log_file, cursor_file)
        assert written_second == 0

    def test_processes_only_new_events_on_incremental_run(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)

        # Append new event
        with open(events_file, "a") as f:
            f.write(json.dumps(REJECT_EVENT) + "\n")

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 1
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[2])["action"] == "reject"

    def test_skips_malformed_lines(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        events_file.write_text(
            json.dumps(DISPATCH_EVENT) + "\n"
            + "NOT_JSON\n"
            + json.dumps(WAIT_EVENT) + "\n"
        )
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 2

    def test_dry_run_does_not_write_log(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file, dry_run=True)
        assert written == 1
        assert not log_file.exists()

    def test_dry_run_does_not_advance_cursor(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file, dry_run=True)
        assert load_cursor(cursor_file) == 0

    def test_dry_run_prints_json_to_stdout(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file, dry_run=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["action"] == "dispatch"

    def test_missing_events_file_returns_zero(self, tmp_path):
        written = process_events_file(
            tmp_path / "nonexistent.ndjson",
            tmp_path / "log.jsonl",
            tmp_path / "cursor.json",
        )
        assert written == 0

    def test_empty_events_file_returns_zero(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        events_file.write_text("")
        written = process_events_file(events_file, tmp_path / "log.jsonl", tmp_path / "cursor.json")
        assert written == 0

    def test_action_ordering_preserved(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT, REJECT_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        lines = log_file.read_text().strip().splitlines()
        actions = [json.loads(l)["action"] for l in lines]
        assert actions == ["dispatch", "wait", "reject"]

    def test_unknown_event_types_skipped(self, tmp_path):
        events = [
            {"event_type": "some_internal_event", "timestamp": "2026-04-22T10:00:00+00:00"},
            WAIT_EVENT,
        ]
        events_file = make_events_file(tmp_path, events)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 1
        assert json.loads(log_file.read_text().strip())["action"] == "wait"

    def test_partial_trailing_line_not_skipped(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"

        N = 3
        valid_lines = [
            json.dumps(DISPATCH_EVENT),
            json.dumps(WAIT_EVENT),
            json.dumps(REJECT_EVENT),
        ]
        # Partial last line — incomplete JSON, no trailing newline
        partial = '{"event_type": "t0_escalate", "reason": "partial'
        events_file.write_text("\n".join(valid_lines) + "\n" + partial)

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == N, f"Expected {N} records written, got {written}"
        cursor_val = load_cursor(cursor_file)
        assert cursor_val == N, f"Cursor should stop at {N} (not past partial), got {cursor_val}"

        # Complete the partial line by appending and re-run
        with open(events_file, "a", encoding="utf-8") as f:
            f.write('", "timestamp": "2026-04-22T10:39:00+00:00"}\n')

        written2 = process_events_file(events_file, log_file, cursor_file)
        assert written2 == 1, f"Expected 1 new record after completing partial, got {written2}"
        cursor_val2 = load_cursor(cursor_file)
        assert cursor_val2 == N + 1, f"Cursor should be {N + 1}, got {cursor_val2}"


# ---------------------------------------------------------------------------
# process_events_file — cursor staleness recovery
# ---------------------------------------------------------------------------

class TestProcessEventsFileCursorStaleness:
    def test_stale_cursor_resets_to_start(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        cursor_file = tmp_path / "cursor.json"
        log_file = tmp_path / "log.jsonl"
        events_file.write_text(json.dumps(DISPATCH_EVENT) + "\n")
        save_cursor(cursor_file, 999)

        written = process_events_file(events_file, log_file, cursor_file)

        assert written == 1
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "dispatch"

    def test_stale_cursor_advances_cursor_to_file_length(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        cursor_file = tmp_path / "cursor.json"
        log_file = tmp_path / "log.jsonl"
        events_file.write_text(json.dumps(WAIT_EVENT) + "\n")
        save_cursor(cursor_file, 50)

        process_events_file(events_file, log_file, cursor_file)

        assert load_cursor(cursor_file) == 1

    def test_exact_match_cursor_does_not_reset(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        cursor_file = tmp_path / "cursor.json"
        log_file = tmp_path / "log.jsonl"
        process_events_file(events_file, log_file, cursor_file)
        cursor_after_first = load_cursor(cursor_file)

        written_second = process_events_file(events_file, log_file, cursor_file)

        assert written_second == 0
        assert load_cursor(cursor_file) == cursor_after_first

    def test_cursor_resets_when_source_replaced_same_length(self, tmp_path):
        # Process initial 3-event file so cursor = 3 with inode recorded
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT, COMPLETE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        assert load_cursor(cursor_file) == 3

        # Replace source with different content but same line count (new inode)
        events_file.unlink()
        new_events = [REJECT_EVENT, ESCALATE_EVENT, UNKNOWN_DECISION_EVENT]
        events_file.write_text("\n".join(json.dumps(e) for e in new_events) + "\n")
        log_file.unlink()

        # Inode mismatch must trigger cursor reset; all 3 new lines processed
        written = process_events_file(events_file, log_file, cursor_file)

        assert written == 3
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["action"] == "reject"


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_0_on_missing_events_file(self, tmp_path):
        rc = main([
            "--events-file", str(tmp_path / "nonexistent.ndjson"),
            "--decision-log", str(tmp_path / "log.jsonl"),
            "--cursor-file", str(tmp_path / "cursor.json"),
        ])
        assert rc == 0

    def test_returns_0_on_empty_events_file(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        events_file.write_text("")
        rc = main([
            "--events-file", str(events_file),
            "--decision-log", str(tmp_path / "log.jsonl"),
            "--cursor-file", str(tmp_path / "cursor.json"),
        ])
        assert rc == 0

    def test_processes_events_and_returns_0(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        rc = main([
            "--events-file", str(events_file),
            "--decision-log", str(log_file),
            "--cursor-file", str(cursor_file),
        ])
        assert rc == 0
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_dry_run_flag_does_not_write(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        rc = main([
            "--events-file", str(events_file),
            "--decision-log", str(log_file),
            "--cursor-file", str(cursor_file),
            "--dry-run",
        ])
        assert rc == 0
        assert not log_file.exists()
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["action"] == "dispatch"
