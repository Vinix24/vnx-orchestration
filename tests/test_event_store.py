#!/usr/bin/env python3
"""Tests for EventStore — NDJSON persistence for agent stream events."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Add scripts/lib to path
SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from event_store import EventStore


@pytest.fixture
def tmp_events_dir(tmp_path):
    """Provide a temp directory for event storage."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    return events_dir


@pytest.fixture
def store(tmp_events_dir):
    """Provide an EventStore instance with temp directory."""
    return EventStore(events_dir=tmp_events_dir)


class TestAppend:
    def test_append_creates_file(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {"session_id": "abc"}})
        path = tmp_events_dir / "T1.ndjson"
        assert path.exists()

    def test_append_writes_valid_ndjson(self, store, tmp_events_dir):
        store.append("T1", {"type": "thinking", "data": {"thinking": "test"}})
        path = tmp_events_dir / "T1.ndjson"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["type"] == "thinking"
        assert event["terminal"] == "T1"
        assert event["sequence"] == 1
        assert "timestamp" in event

    def test_append_multiple_events_sequential(self, store, tmp_events_dir):
        for i in range(5):
            store.append("T1", {"type": "text", "data": {"text": f"msg-{i}"}})
        path = tmp_events_dir / "T1.ndjson"
        lines = [l for l in path.read_text().strip().split("\n") if l]
        assert len(lines) == 5
        for i, line in enumerate(lines):
            event = json.loads(line)
            assert event["sequence"] == i + 1

    def test_append_separate_terminals(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})
        store.append("T2", {"type": "init", "data": {}})
        store.append("T1", {"type": "text", "data": {}})
        assert (tmp_events_dir / "T1.ndjson").exists()
        assert (tmp_events_dir / "T2.ndjson").exists()
        t1_lines = [l for l in (tmp_events_dir / "T1.ndjson").read_text().strip().split("\n") if l]
        t2_lines = [l for l in (tmp_events_dir / "T2.ndjson").read_text().strip().split("\n") if l]
        assert len(t1_lines) == 2
        assert len(t2_lines) == 1

    def test_append_includes_dispatch_id(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}}, dispatch_id="d-001")
        path = tmp_events_dir / "T1.ndjson"
        event = json.loads(path.read_text().strip())
        assert event["dispatch_id"] == "d-001"

    def test_append_kwarg_wins_over_event_dispatch_id(self, store, tmp_events_dir):
        """OI-1349: explicit dispatch_id kwarg takes precedence over event field."""
        from canonical_event import CanonicalEvent
        ce = CanonicalEvent(
            dispatch_id="event-dispatch",
            terminal_id="T1",
            provider="claude",
            event_type="text",
            data={"text": "hi"},
        )
        store.append("T1", ce, dispatch_id="kwarg-dispatch")
        path = tmp_events_dir / "T1.ndjson"
        envelope = json.loads(path.read_text().strip())
        assert envelope["dispatch_id"] == "kwarg-dispatch"

    def test_append_canonical_event_no_kwarg_uses_event_dispatch_id(self, store, tmp_events_dir):
        """When dispatch_id kwarg is omitted, CanonicalEvent.dispatch_id is used."""
        from canonical_event import CanonicalEvent
        ce = CanonicalEvent(
            dispatch_id="event-owns-this",
            terminal_id="T1",
            provider="claude",
            event_type="text",
            data={},
        )
        store.append("T1", ce)
        path = tmp_events_dir / "T1.ndjson"
        envelope = json.loads(path.read_text().strip())
        assert envelope["dispatch_id"] == "event-owns-this"


class TestTail:
    def test_tail_returns_all_events(self, store):
        for i in range(3):
            store.append("T1", {"type": "text", "data": {"text": f"msg-{i}"}})
        events = list(store.tail("T1"))
        assert len(events) == 3

    def test_tail_with_since_filter(self, store):
        store.append("T1", {"type": "text", "data": {"text": "first"}})
        events_before = list(store.tail("T1"))
        first_ts = events_before[0]["timestamp"]

        time.sleep(0.01)  # ensure different timestamp
        store.append("T1", {"type": "text", "data": {"text": "second"}})
        store.append("T1", {"type": "text", "data": {"text": "third"}})

        events_after = list(store.tail("T1", since=first_ts))
        assert len(events_after) == 2
        for e in events_after:
            assert e["timestamp"] > first_ts

    def test_tail_empty_terminal(self, store):
        events = list(store.tail("T1"))
        assert events == []

    def test_tail_nonexistent_terminal(self, store):
        events = list(store.tail("T99"))
        assert events == []

    def test_tail_preserves_order(self, store):
        for i in range(10):
            store.append("T1", {"type": "text", "data": {"index": i}})
        events = list(store.tail("T1"))
        for i, event in enumerate(events):
            assert event["sequence"] == i + 1


class TestClear:
    def test_clear_removes_events(self, store, tmp_events_dir):
        store.append("T1", {"type": "text", "data": {}})
        store.append("T1", {"type": "text", "data": {}})
        assert store.event_count("T1") == 2

        store.clear("T1")
        assert store.event_count("T1") == 0
        assert (tmp_events_dir / "T1.ndjson").exists()  # file still exists, just empty

    def test_clear_resets_sequence(self, store):
        store.append("T1", {"type": "text", "data": {}})
        store.append("T1", {"type": "text", "data": {}})
        store.clear("T1")
        store.append("T1", {"type": "text", "data": {}})
        events = list(store.tail("T1"))
        assert len(events) == 1
        assert events[0]["sequence"] == 1

    def test_clear_nonexistent_terminal(self, store):
        # Should not raise
        store.clear("T99")

    def test_clear_does_not_affect_other_terminals(self, store):
        store.append("T1", {"type": "text", "data": {}})
        store.append("T2", {"type": "text", "data": {}})
        store.clear("T1")
        assert store.event_count("T1") == 0
        assert store.event_count("T2") == 1


class TestConcurrentWrites:
    def test_concurrent_appends_no_corruption(self, store, tmp_events_dir):
        """Multiple threads appending should produce valid NDJSON without corruption."""
        errors = []
        n_threads = 4
        n_events_per_thread = 25

        def writer(thread_id):
            try:
                for i in range(n_events_per_thread):
                    store.append("T1", {
                        "type": "text",
                        "data": {"thread": thread_id, "index": i},
                    })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        # Verify all lines are valid JSON
        path = tmp_events_dir / "T1.ndjson"
        lines = [l for l in path.read_text().strip().split("\n") if l]
        assert len(lines) == n_threads * n_events_per_thread
        for line in lines:
            event = json.loads(line)  # should not raise
            assert "type" in event
            assert "sequence" in event


class TestArchive:
    def test_clear_with_archive_creates_archive_file(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}}, dispatch_id="d-100")
        store.append("T1", {"type": "result", "data": {}}, dispatch_id="d-100")
        assert store.event_count("T1") == 2

        store.clear("T1", archive_dispatch_id="d-100")
        assert store.event_count("T1") == 0

        archive_path = tmp_events_dir / "archive" / "T1" / "d-100.ndjson"
        assert archive_path.exists()
        lines = [l for l in archive_path.read_text().strip().split("\n") if l]
        assert len(lines) == 2
        for line in lines:
            event = json.loads(line)
            assert event["dispatch_id"] == "d-100"

    def test_clear_without_archive_does_not_create_archive(self, store, tmp_events_dir):
        store.append("T1", {"type": "init", "data": {}})
        store.clear("T1")
        archive_dir = tmp_events_dir / "archive" / "T1"
        assert not archive_dir.exists()

    def test_archive_empty_file_returns_none(self, store, tmp_events_dir):
        # Create empty file
        path = tmp_events_dir / "T1.ndjson"
        path.touch()
        result = store.archive("T1", "d-200")
        assert result is None

    def test_archive_nonexistent_file_returns_none(self, store):
        result = store.archive("T1", "d-300")
        assert result is None

    def test_archive_preserves_content(self, store, tmp_events_dir):
        for i in range(5):
            store.append("T1", {"type": "text", "data": {"i": i}}, dispatch_id="d-400")
        store.clear("T1", archive_dispatch_id="d-400")

        archive_path = tmp_events_dir / "archive" / "T1" / "d-400.ndjson"
        lines = [l for l in archive_path.read_text().strip().split("\n") if l]
        assert len(lines) == 5
        for i, line in enumerate(lines):
            event = json.loads(line)
            assert event["sequence"] == i + 1

    def test_archive_dir_property(self, store, tmp_events_dir):
        expected = tmp_events_dir / "archive" / "T1"
        assert store.archive_dir("T1") == expected


class TestEventCount:
    def test_event_count_zero(self, store):
        assert store.event_count("T1") == 0

    def test_event_count_matches(self, store):
        for _ in range(7):
            store.append("T1", {"type": "text", "data": {}})
        assert store.event_count("T1") == 7


class TestLastEvent:
    def test_last_event_none_when_empty(self, store):
        assert store.last_event("T1") is None

    def test_last_event_returns_final(self, store):
        store.append("T1", {"type": "text", "data": {"text": "first"}})
        store.append("T1", {"type": "result", "data": {"cost": 0.01}})
        last = store.last_event("T1")
        assert last["type"] == "result"


# ---------------------------------------------------------------------------
# Size-based rotation: the live stream is archived + truncated at the threshold
# (replaces the old log-only "operator intervention recommended" warning)
# ---------------------------------------------------------------------------


def _ndjson_lines(path: Path):
    if not path.exists():
        return []
    return [line for line in path.read_text().strip().split("\n") if line]


class TestSizeRotation:
    def test_no_rotation_below_threshold(self, tmp_events_dir):
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=10_000_000)
        for i in range(3):
            store.append("T1", {"type": "text", "data": {"i": i}}, dispatch_id="d-rot-below")
        assert store.event_count("T1") == 3
        assert not (tmp_events_dir / "archive" / "T1").exists(), (
            "below threshold there must be no archive and no rotation"
        )

    def test_rotation_above_threshold(self, tmp_events_dir):
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=500)
        payload = "x" * 200
        for i in range(20):
            store.append(
                "T1",
                {"type": "text", "data": {"i": i, "padding": payload}},
                dispatch_id="d-rot-above",
            )
        # Live file is bounded well under the ~4KB written; rotation fired.
        live = tmp_events_dir / "T1.ndjson"
        assert live.stat().st_size <= 500 + 1000
        archives = list((tmp_events_dir / "archive" / "T1").glob("size-rotation-*.ndjson"))
        assert len(archives) >= 1, "rotation must archive the oversize stream"

    def test_rotated_archive_contains_old_content(self, tmp_events_dir):
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=500)
        payload = "y" * 300
        for i in range(20):
            store.append(
                "T1",
                {"type": "text", "data": {"i": i, "padding": payload}},
                dispatch_id="d-rot-content",
            )
        archived_lines = []
        for arc in (tmp_events_dir / "archive" / "T1").glob("size-rotation-*.ndjson"):
            archived_lines.extend(_ndjson_lines(arc))
        assert archived_lines, "the rotated file must contain the pre-rotation events"
        for line in archived_lines:
            event = json.loads(line)
            assert event["dispatch_id"] == "d-rot-content"
            assert event["type"] == "text"
        # No event is lost: archive + live == everything written.
        live_lines = _ndjson_lines(tmp_events_dir / "T1.ndjson")
        assert len(archived_lines) + len(live_lines) == 20

    def test_live_file_empty_and_writable_after_rotation(self, tmp_events_dir):
        # First event is big enough to cross the threshold and trigger rotation.
        # The follow-up event is small enough to stay below it.
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=300)
        store.append(
            "T1",
            {"type": "text", "data": {"padding": "z" * 400}},
            dispatch_id="d-rot-writable",
        )
        assert store.event_count("T1") == 0, "live file must be empty after rotation"
        store.append("T1", {"type": "result", "data": {}}, dispatch_id="d-rot-writable")
        events = list(store.tail("T1"))
        assert len(events) == 1
        assert events[0]["sequence"] == 1
        assert events[0]["type"] == "result"

    def test_concurrent_writer_loses_nothing(self, tmp_events_dir):
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=2000)
        n_threads = 4
        n_events = 40
        payload = "p" * 120  # ~150B/event -> ~24KB total, many rotations
        errors = []

        def writer(tid):
            try:
                for i in range(n_events):
                    store.append(
                        "T1",
                        {"type": "text", "data": {"t": tid, "i": i, "padding": payload}},
                        dispatch_id="d-rot-concurrent",
                    )
            except Exception as exc:  # vnx-silent-except: collect and assert, don't let a thread die silently
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"thread errors: {errors}"

        total = len(_ndjson_lines(tmp_events_dir / "T1.ndjson"))
        archive_dir = tmp_events_dir / "archive" / "T1"
        if archive_dir.exists():
            for arc in archive_dir.glob("*.ndjson"):
                total += len(_ndjson_lines(arc))
        assert total == n_threads * n_events, (
            f"a concurrent writer lost events: expected {n_threads * n_events}, got {total}"
        )

    def test_rotation_failure_logs_error_and_does_not_block(self, tmp_events_dir, caplog):
        import logging

        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=100)
        # Make the archive dir un-creatable: archive/T1 is a regular file.
        archive_dir = tmp_events_dir / "archive"
        archive_dir.mkdir()
        (archive_dir / "T1").write_text("not a directory")

        caplog.set_level(logging.ERROR, logger="event_store")

        payload = "f" * 200
        for i in range(5):
            store.append(
                "T1",
                {"type": "text", "data": {"i": i, "padding": payload}},
                dispatch_id="d-rot-fail",
            )

        # The dispatch must keep going: append does not raise, live file intact.
        assert store.event_count("T1") == 5
        errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and "size rotation failed" in r.message
        ]
        assert len(errors) >= 1, "a failed rotation must log a visible ERROR"
        assert store.oversize_flags() != [], "a failed rotation must write the oversize flag"


class TestSizeRotationConfig:
    def test_default_threshold_is_10mb(self, tmp_events_dir):
        store = EventStore(events_dir=tmp_events_dir)
        assert store._rotation_threshold() == 10 * 1024 * 1024

    def test_env_var_overrides_default(self, tmp_events_dir, monkeypatch):
        monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "1234")
        store = EventStore(events_dir=tmp_events_dir)
        assert store._rotation_threshold() == 1234

    def test_ctor_arg_overrides_env(self, tmp_events_dir, monkeypatch):
        monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "1234")
        store = EventStore(events_dir=tmp_events_dir, rotation_threshold_bytes=99)
        assert store._rotation_threshold() == 99

    def test_invalid_env_falls_back_to_default(self, tmp_events_dir, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "not-a-number")
        caplog.set_level(logging.WARNING, logger="event_store")
        store = EventStore(events_dir=tmp_events_dir)
        assert store._rotation_threshold() == 10 * 1024 * 1024
