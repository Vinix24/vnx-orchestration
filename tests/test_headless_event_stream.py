#!/usr/bin/env python3
"""Headless event stream and artifact correlation tests for PR-2.

Covers:
  1. Event emission and canonical order
  2. Correlation key consistency
  3. Artifact materialization and tracking
  4. Terminal event semantics
  5. Timeline reconstruction (NDJSON)
  6. Validation helpers
  7. Full lifecycle reconstruction
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from headless_event_stream import (
    CANONICAL_ORDER,
    TERMINAL_EVENT_TYPES,
    VALID_EVENT_TYPES,
    CorrelationKeys,
    HeadlessEvent,
    HeadlessEventStream,
)


# ---------------------------------------------------------------------------
# 1. Event emission and canonical order
# ---------------------------------------------------------------------------

class TestEventEmission:

    def test_emit_session_created(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1")
        event = stream.emit("session_created")
        assert event.event_type == "session_created"
        assert event.correlation.session_id == "T1"
        assert event.correlation.dispatch_id == "d-1"

    def test_emit_sequence_follows_canonical_order(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1")
        stream.emit("session_created")
        stream.emit("subprocess_launched", details={"pid": 1234})
        stream.emit("heartbeat")
        stream.emit("session_completed", details={"exit_code": 0})
        violations = stream.validate_order()
        assert violations == []

    def test_first_event_must_be_session_created(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("subprocess_launched")
        violations = stream.validate_order()
        assert any("session_created" in v for v in violations)

    def test_unknown_event_type_rejected(self) -> None:
        stream = HeadlessEventStream("T1")
        with pytest.raises(ValueError, match="Unknown event type"):
            stream.emit("invalid_type")

    def test_events_have_timestamps(self) -> None:
        stream = HeadlessEventStream("T1")
        event = stream.emit("session_created")
        assert event.timestamp is not None
        assert "T" in event.timestamp  # ISO format


# ---------------------------------------------------------------------------
# 2. Correlation key consistency
# ---------------------------------------------------------------------------

class TestCorrelation:

    def test_all_events_share_session_id(self) -> None:
        stream = HeadlessEventStream("T2", dispatch_id="d-2", attempt_number=3)
        stream.emit("session_created")
        stream.emit("heartbeat")
        stream.emit("session_completed")
        violations = stream.validate_correlation()
        assert violations == []

    def test_attempt_id_format(self) -> None:
        keys = CorrelationKeys(session_id="T1", attempt_number=2, dispatch_id="d-1")
        assert keys.attempt_id == "T1-attempt-2"

    def test_correlation_keys_in_event_dict(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1", attempt_number=5)
        event = stream.emit("session_created")
        d = event.to_dict()
        assert d["session_id"] == "T1"
        assert d["attempt_id"] == "T1-attempt-5"
        assert d["attempt_number"] == 5
        assert d["dispatch_id"] == "d-1"


# ---------------------------------------------------------------------------
# 3. Artifact materialization
# ---------------------------------------------------------------------------

class TestArtifactCorrelation:

    def test_record_artifact_emits_event(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        event = stream.record_artifact("report", "/tmp/report.md")
        assert event.event_type == "artifact_materialized"
        assert event.artifact_path == "/tmp/report.md"
        assert event.details["artifact_name"] == "report"

    def test_artifacts_tracked(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.record_artifact("report", "/tmp/report.md")
        stream.record_artifact("receipt", "/tmp/receipt.json")
        assert stream.artifacts == {
            "report": "/tmp/report.md",
            "receipt": "/tmp/receipt.json",
        }

    def test_artifact_event_has_correlation(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1")
        stream.emit("session_created")
        event = stream.record_artifact("output", "/out.txt")
        assert event.correlation.session_id == "T1"
        assert event.correlation.dispatch_id == "d-1"


# ---------------------------------------------------------------------------
# 4. Terminal event semantics
# ---------------------------------------------------------------------------

class TestTerminalEvents:

    def test_completed_terminates_stream(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.emit("session_completed")
        assert stream.is_terminated is True

    def test_failed_terminates_stream(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.emit("session_failed", details={"reason": "exit 1"})
        assert stream.is_terminated is True

    def test_timed_out_terminates_stream(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.emit("session_timed_out")
        assert stream.is_terminated is True

    def test_emit_after_terminal_raises(self) -> None:
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.emit("session_completed")
        with pytest.raises(ValueError, match="terminated"):
            stream.emit("heartbeat")

    def test_event_after_terminal_detected_by_validate(self) -> None:
        """Stream.emit prevents this, but validate_order catches it in loaded data."""
        stream = HeadlessEventStream("T1")
        stream.emit("session_created")
        stream.emit("session_completed")
        # Manually append (bypass emit guard) to test validator
        from headless_event_stream import HeadlessEvent, _now_iso
        stream._events.append(HeadlessEvent(
            event_type="heartbeat", timestamp=_now_iso(),
            correlation=stream.correlation,
        ))
        stream._terminated = False  # reset for validation
        violations = stream.validate_order()
        assert any("after terminal" in v for v in violations)


# ---------------------------------------------------------------------------
# 5. Timeline reconstruction (NDJSON)
# ---------------------------------------------------------------------------

class TestTimelineReconstruction:

    def test_timeline_returns_ordered_dicts(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1")
        stream.emit("session_created")
        stream.emit("subprocess_launched", details={"pid": 99})
        stream.emit("session_completed")
        timeline = stream.timeline()
        assert len(timeline) == 3
        assert timeline[0]["event_type"] == "session_created"
        assert timeline[1]["details"]["pid"] == 99
        assert timeline[2]["event_type"] == "session_completed"

    def test_ndjson_serialization(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1")
        stream.emit("session_created")
        stream.emit("session_completed")
        ndjson = stream.to_ndjson()
        lines = ndjson.strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "event_type" in parsed
            assert "session_id" in parsed

    def test_ndjson_roundtrip(self) -> None:
        stream = HeadlessEventStream("T1", dispatch_id="d-1", attempt_number=2)
        stream.emit("session_created")
        stream.emit("heartbeat", details={"progress": 50})
        stream.record_artifact("report", "/tmp/r.md")
        stream.emit("session_completed")
        ndjson = stream.to_ndjson()
        events = [json.loads(line) for line in ndjson.split("\n")]
        assert len(events) == 4
        assert events[2]["artifact_path"] == "/tmp/r.md"
        assert all(e["session_id"] == "T1" for e in events)
        assert all(e["attempt_number"] == 2 for e in events)


# ---------------------------------------------------------------------------
# 6. Validation helpers
# ---------------------------------------------------------------------------

class TestValidation:

    def test_valid_event_types_complete(self) -> None:
        assert "session_created" in VALID_EVENT_TYPES
        assert "session_completed" in VALID_EVENT_TYPES
        assert "session_failed" in VALID_EVENT_TYPES
        assert "session_timed_out" in VALID_EVENT_TYPES
        assert "heartbeat" in VALID_EVENT_TYPES

    def test_terminal_types_subset_of_valid(self) -> None:
        assert TERMINAL_EVENT_TYPES.issubset(VALID_EVENT_TYPES)

    def test_empty_stream_validates(self) -> None:
        stream = HeadlessEventStream("T1")
        assert stream.validate_order() == []
        assert stream.validate_correlation() == []


# ---------------------------------------------------------------------------
# 7. Full lifecycle reconstruction
# ---------------------------------------------------------------------------

class TestFullLifecycle:

    def test_complete_headless_run(self) -> None:
        stream = HeadlessEventStream("T2", dispatch_id="20260403-120000-test", attempt_number=1)
        stream.emit("session_created", details={"command": "claude --model opus"})
        stream.emit("subprocess_launched", details={"pid": 12345})
        stream.emit("heartbeat", details={"progress": 25})
        stream.emit("heartbeat", details={"progress": 75})
        stream.record_artifact("report", "/tmp/unified_reports/report.md")
        stream.emit("session_completed", details={"exit_code": 0})

        assert stream.is_terminated
        assert len(stream.events) == 6
        assert stream.artifacts == {"report": "/tmp/unified_reports/report.md"}
        assert stream.validate_order() == []
        assert stream.validate_correlation() == []
        timeline = stream.timeline()
        assert timeline[0]["event_type"] == "session_created"
        assert timeline[-1]["event_type"] == "session_completed"

    def test_failed_run_with_artifacts(self) -> None:
        stream = HeadlessEventStream("T3", dispatch_id="d-fail", attempt_number=2)
        stream.emit("session_created")
        stream.emit("subprocess_launched", details={"pid": 9999})
        stream.record_artifact("partial_output", "/tmp/partial.txt")
        stream.emit("session_failed", details={"exit_code": 1, "reason": "test failure"})

        assert stream.is_terminated
        assert len(stream.events) == 4
        assert "partial_output" in stream.artifacts
        ndjson = stream.to_ndjson()
        events = [json.loads(l) for l in ndjson.split("\n")]
        assert events[-1]["event_type"] == "session_failed"
        assert events[-1]["dispatch_id"] == "d-fail"
