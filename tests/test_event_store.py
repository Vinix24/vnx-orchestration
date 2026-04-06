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
