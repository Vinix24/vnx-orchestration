#!/usr/bin/env python3
"""Tests for t0_escalations_log.py — passive escalation JSONL writer.

Covers:
- build_record: schema conformance and defaults
- record_from_executor_event: t0_escalate event → record; non-escalation → None
- record_from_governance_transition: governance state transition → record
- write_escalation: JSONL append with file locking, directory creation
- load_cursor / save_cursor: round-trip, missing file, corrupt file
- process_events_file: cursor advancement, idempotency, dry-run, empty/missing file,
                        partial trailing line, cursor staleness recovery
- load_recent_escalations: empty log, limit, ordering
- main() CLI: dry-run, missing events file (no-op, exit 0)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from t0_escalations_log import (
    DEFAULT_CURSOR_FILE,
    DEFAULT_ESCALATION_LOG,
    DEFAULT_EVENTS_FILE,
    build_record,
    load_cursor,
    load_recent_escalations,
    main,
    process_events_file,
    record_from_executor_event,
    record_from_governance_transition,
    save_cursor,
    write_escalation,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ESCALATE_EVENT = {
    "event_type": "t0_escalate",
    "reason": "Architectural blocker: merge conflict unresolvable",
    "timestamp": "2026-04-22T11:55:00+00:00",
}

ESCALATE_EVENT_NO_REASON = {
    "event_type": "t0_escalate",
    "timestamp": "2026-04-22T11:56:00+00:00",
}

DISPATCH_EVENT = {
    "event_type": "t0_dispatch",
    "dispatch_id": "20260422-A",
    "dispatch_target": "T1",
    "timestamp": "2026-04-22T11:54:00+00:00",
}

WAIT_EVENT = {
    "event_type": "t0_wait",
    "reason": "Waiting for Track B",
    "timestamp": "2026-04-22T11:53:00+00:00",
}

MIXED_EVENTS = [DISPATCH_EVENT, WAIT_EVENT, ESCALATE_EVENT]


def make_events_file(tmp_path: Path, events: list[dict]) -> Path:
    f = tmp_path / "t0_decisions.ndjson"
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return f


# ---------------------------------------------------------------------------
# build_record
# ---------------------------------------------------------------------------

class TestBuildRecord:
    def test_required_schema_keys_present(self):
        r = build_record("escalate")
        for key in (
            "timestamp", "entity_type", "entity_id", "escalation_level",
            "from_level", "trigger_category", "trigger_description", "actor", "source",
        ):
            assert key in r, f"Missing key: {key}"

    def test_escalation_level_preserved(self):
        for level in ("info", "review_required", "hold", "escalate"):
            r = build_record(level)
            assert r["escalation_level"] == level

    def test_defaults_for_optional_fields(self):
        r = build_record("escalate")
        assert r["entity_type"] is None
        assert r["entity_id"] is None
        assert r["from_level"] is None
        assert r["trigger_category"] is None
        assert r["trigger_description"] is None
        assert r["actor"] == "runtime"
        assert r["source"] == "executor"

    def test_custom_timestamp_used(self):
        r = build_record("hold", timestamp="2026-01-01T00:00:00+00:00")
        assert r["timestamp"] == "2026-01-01T00:00:00+00:00"

    def test_timestamp_generated_when_absent(self):
        r = build_record("escalate")
        assert "T" in r["timestamp"]

    def test_all_fields_passed_through(self):
        r = build_record(
            "hold",
            entity_type="dispatch",
            entity_id="abc-123",
            from_level="review_required",
            trigger_category="repeated_failure",
            trigger_description="Too many retries",
            actor="t0",
            source="governance",
        )
        assert r["entity_type"] == "dispatch"
        assert r["entity_id"] == "abc-123"
        assert r["from_level"] == "review_required"
        assert r["trigger_category"] == "repeated_failure"
        assert r["trigger_description"] == "Too many retries"
        assert r["actor"] == "t0"
        assert r["source"] == "governance"


# ---------------------------------------------------------------------------
# record_from_executor_event
# ---------------------------------------------------------------------------

class TestRecordFromExecutorEvent:
    def test_t0_escalate_returns_record(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r is not None

    def test_t0_escalate_level_is_escalate(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r["escalation_level"] == "escalate"

    def test_t0_escalate_actor_is_t0(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r["actor"] == "t0"

    def test_t0_escalate_source_is_executor(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r["source"] == "executor"

    def test_t0_escalate_reason_in_trigger_description(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert "merge conflict" in r["trigger_description"]

    def test_t0_escalate_timestamp_preserved(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r["timestamp"] == "2026-04-22T11:55:00+00:00"

    def test_t0_escalate_missing_reason_uses_default(self):
        r = record_from_executor_event(ESCALATE_EVENT_NO_REASON)
        assert r is not None
        assert "Escalation triggered" in r["trigger_description"]

    def test_entity_fields_are_none(self):
        r = record_from_executor_event(ESCALATE_EVENT)
        assert r["entity_type"] is None
        assert r["entity_id"] is None
        assert r["from_level"] is None

    def test_non_escalate_event_returns_none(self):
        assert record_from_executor_event(DISPATCH_EVENT) is None
        assert record_from_executor_event(WAIT_EVENT) is None

    def test_unknown_event_type_returns_none(self):
        assert record_from_executor_event({"event_type": "some_internal"}) is None

    def test_missing_event_type_returns_none(self):
        assert record_from_executor_event({}) is None


# ---------------------------------------------------------------------------
# record_from_governance_transition
# ---------------------------------------------------------------------------

class TestRecordFromGovernanceTransition:
    def test_returns_record_with_correct_level(self):
        r = record_from_governance_transition(
            entity_type="dispatch",
            entity_id="abc-123",
            from_level="info",
            new_level="hold",
            actor="runtime",
        )
        assert r["escalation_level"] == "hold"

    def test_source_is_governance(self):
        r = record_from_governance_transition(
            entity_type="dispatch",
            entity_id="abc-123",
            from_level="info",
            new_level="review_required",
            actor="runtime",
        )
        assert r["source"] == "governance"

    def test_entity_fields_preserved(self):
        r = record_from_governance_transition(
            entity_type="feature",
            entity_id="F36",
            from_level="review_required",
            new_level="escalate",
            actor="t0",
        )
        assert r["entity_type"] == "feature"
        assert r["entity_id"] == "F36"
        assert r["from_level"] == "review_required"
        assert r["actor"] == "t0"

    def test_trigger_fields_preserved(self):
        r = record_from_governance_transition(
            entity_type="dispatch",
            entity_id="abc",
            from_level="info",
            new_level="hold",
            actor="runtime",
            trigger_category="repeated_failure",
            trigger_description="Exceeded retry limit",
        )
        assert r["trigger_category"] == "repeated_failure"
        assert r["trigger_description"] == "Exceeded retry limit"

    def test_custom_timestamp_used(self):
        r = record_from_governance_transition(
            entity_type="dispatch",
            entity_id="abc",
            from_level="info",
            new_level="hold",
            actor="runtime",
            timestamp="2026-04-22T12:00:00+00:00",
        )
        assert r["timestamp"] == "2026-04-22T12:00:00+00:00"

    def test_timestamp_generated_when_absent(self):
        r = record_from_governance_transition(
            entity_type="dispatch",
            entity_id="abc",
            from_level="info",
            new_level="hold",
            actor="runtime",
        )
        assert "T" in r["timestamp"]


# ---------------------------------------------------------------------------
# write_escalation
# ---------------------------------------------------------------------------

class TestWriteEscalation:
    def test_writes_valid_jsonl_line(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        record = build_record("escalate", trigger_description="blocker", actor="t0")
        write_escalation(record, log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["escalation_level"] == "escalate"
        assert parsed["actor"] == "t0"

    def test_appends_multiple_records(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        for level in ("info", "review_required", "hold", "escalate"):
            write_escalation(build_record(level), log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 4
        assert json.loads(lines[3])["escalation_level"] == "escalate"

    def test_creates_parent_directory(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "log.jsonl"
        write_escalation(build_record("hold"), log_file)
        assert log_file.exists()

    def test_appends_to_existing_file(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        log_file.write_text('{"escalation_level":"prior"}\n')
        write_escalation(build_record("hold"), log_file)
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["escalation_level"] == "prior"
        assert json.loads(lines[1])["escalation_level"] == "hold"

    def test_each_line_is_valid_json(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        for level in ("info", "review_required", "hold", "escalate"):
            write_escalation(build_record(level), log_file)
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
    def test_only_escalate_events_converted(self, tmp_path):
        events_file = make_events_file(tmp_path, MIXED_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 1
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["escalation_level"] == "escalate"

    def test_multiple_escalate_events_all_written(self, tmp_path):
        events = [ESCALATE_EVENT, DISPATCH_EVENT, ESCALATE_EVENT_NO_REASON]
        events_file = make_events_file(tmp_path, events)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 2
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_cursor_advanced_after_processing(self, tmp_path):
        events_file = make_events_file(tmp_path, MIXED_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        assert load_cursor(cursor_file) > 0

    def test_idempotent_on_second_run(self, tmp_path):
        events_file = make_events_file(tmp_path, MIXED_EVENTS)
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        written_second = process_events_file(events_file, log_file, cursor_file)
        assert written_second == 0

    def test_processes_only_new_escalations_on_incremental_run(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)

        with open(events_file, "a") as f:
            f.write(json.dumps(ESCALATE_EVENT) + "\n")

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 1
        assert json.loads(log_file.read_text().strip())["escalation_level"] == "escalate"

    def test_skips_malformed_lines(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        events_file.write_text(
            json.dumps(ESCALATE_EVENT) + "\n"
            + "NOT_JSON\n"
            + json.dumps(ESCALATE_EVENT_NO_REASON) + "\n"
        )
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 2

    def test_dry_run_does_not_write_log(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        written = process_events_file(events_file, log_file, cursor_file, dry_run=True)
        assert written == 1
        assert not log_file.exists()

    def test_dry_run_does_not_advance_cursor(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file, dry_run=True)
        assert load_cursor(cursor_file) == 0

    def test_dry_run_prints_json_to_stdout(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file, dry_run=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["escalation_level"] == "escalate"

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

    def test_no_escalate_events_returns_zero(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        written = process_events_file(events_file, tmp_path / "log.jsonl", tmp_path / "cursor.json")
        assert written == 0

    def test_partial_trailing_line_not_advanced(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"

        valid_lines = [
            json.dumps(ESCALATE_EVENT),
            json.dumps(DISPATCH_EVENT),
        ]
        partial = '{"event_type": "t0_escalate", "reason": "partial'
        events_file.write_text("\n".join(valid_lines) + "\n" + partial)

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 1
        cursor_val = load_cursor(cursor_file)
        assert cursor_val == 2  # stopped before partial

        # Complete the partial line
        with open(events_file, "a", encoding="utf-8") as f:
            f.write('", "timestamp": "2026-04-22T11:55:00+00:00"}\n')

        written2 = process_events_file(events_file, log_file, cursor_file)
        assert written2 == 1
        assert load_cursor(cursor_file) == 3


# ---------------------------------------------------------------------------
# process_events_file — cursor staleness recovery
# ---------------------------------------------------------------------------

class TestProcessEventsFileCursorStaleness:
    def test_stale_cursor_resets_to_start(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        save_cursor(cursor_file, 999)

        written = process_events_file(events_file, log_file, cursor_file)

        assert written == 1
        assert json.loads(log_file.read_text().strip())["escalation_level"] == "escalate"

    def test_stale_cursor_advances_to_file_length(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        save_cursor(cursor_file, 50)

        process_events_file(events_file, log_file, cursor_file)
        assert load_cursor(cursor_file) == 1

    def test_cursor_resets_when_source_replaced_same_length(self, tmp_path):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT, ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        process_events_file(events_file, log_file, cursor_file)
        assert load_cursor(cursor_file) == 3

        # Replace with different content, same line count
        events_file.unlink()
        new_events = [ESCALATE_EVENT, ESCALATE_EVENT, ESCALATE_EVENT_NO_REASON]
        events_file.write_text("\n".join(json.dumps(e) for e in new_events) + "\n")
        log_file.unlink()

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 3
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_legacy_cursor_at_eof_upgraded_with_inode(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        cursor_file = tmp_path / "cursor.json"
        log_file = tmp_path / "log.jsonl"

        cursor_file.write_text(json.dumps({"processed_lines": 1}) + "\n")

        written = process_events_file(events_file, log_file, cursor_file)
        assert written == 0

        cursor_state = json.loads(cursor_file.read_text())
        assert "inode" in cursor_state
        assert cursor_state["inode"] != 0
        assert cursor_state["processed_lines"] == 1


# ---------------------------------------------------------------------------
# load_recent_escalations
# ---------------------------------------------------------------------------

class TestLoadRecentEscalations:
    def test_returns_empty_list_when_log_missing(self, tmp_path):
        result = load_recent_escalations(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_returns_all_records_when_under_limit(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        for level in ("info", "review_required"):
            write_escalation(build_record(level), log_file)
        result = load_recent_escalations(log_file, limit=10)
        assert len(result) == 2

    def test_returns_most_recent_n_records(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        levels = ["info", "review_required", "hold", "escalate", "info"]
        for level in levels:
            write_escalation(build_record(level), log_file)
        result = load_recent_escalations(log_file, limit=3)
        assert len(result) == 3
        assert result[-1]["escalation_level"] == "info"

    def test_returns_records_in_order(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        write_escalation(build_record("info"), log_file)
        write_escalation(build_record("hold"), log_file)
        result = load_recent_escalations(log_file, limit=5)
        assert result[0]["escalation_level"] == "info"
        assert result[1]["escalation_level"] == "hold"

    def test_skips_corrupt_lines(self, tmp_path):
        log_file = tmp_path / "log.jsonl"
        log_file.write_text(
            '{"escalation_level":"info"}\n'
            + "CORRUPT\n"
            + '{"escalation_level":"hold"}\n'
        )
        result = load_recent_escalations(log_file)
        assert len(result) == 2
        assert result[0]["escalation_level"] == "info"
        assert result[1]["escalation_level"] == "hold"


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_0_on_missing_events_file(self, tmp_path):
        rc = main([
            "--events-file", str(tmp_path / "nonexistent.ndjson"),
            "--escalation-log", str(tmp_path / "log.jsonl"),
            "--cursor-file", str(tmp_path / "cursor.json"),
        ])
        assert rc == 0

    def test_returns_0_on_empty_events_file(self, tmp_path):
        events_file = tmp_path / "t0_decisions.ndjson"
        events_file.write_text("")
        rc = main([
            "--events-file", str(events_file),
            "--escalation-log", str(tmp_path / "log.jsonl"),
            "--cursor-file", str(tmp_path / "cursor.json"),
        ])
        assert rc == 0

    def test_processes_escalate_events_and_returns_0(self, tmp_path):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT, DISPATCH_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        rc = main([
            "--events-file", str(events_file),
            "--escalation-log", str(log_file),
            "--cursor-file", str(cursor_file),
        ])
        assert rc == 0
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["escalation_level"] == "escalate"

    def test_dry_run_flag_does_not_write(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [ESCALATE_EVENT])
        log_file = tmp_path / "log.jsonl"
        cursor_file = tmp_path / "cursor.json"
        rc = main([
            "--events-file", str(events_file),
            "--escalation-log", str(log_file),
            "--cursor-file", str(cursor_file),
            "--dry-run",
        ])
        assert rc == 0
        assert not log_file.exists()
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["escalation_level"] == "escalate"

    def test_non_escalate_events_produce_no_output(self, tmp_path, capsys):
        events_file = make_events_file(tmp_path, [DISPATCH_EVENT, WAIT_EVENT])
        rc = main([
            "--events-file", str(events_file),
            "--escalation-log", str(tmp_path / "log.jsonl"),
            "--cursor-file", str(tmp_path / "cursor.json"),
            "--dry-run",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
