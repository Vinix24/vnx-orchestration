#!/usr/bin/env python3
"""Tests for _streaming_drainer.py — StreamingDrainerMixin."""

from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from canonical_event import CanonicalEvent
from _streaming_drainer import (
    StreamingDrainerMixin,
    _make_error_event,
    _parse_line,
    _STREAMING_TIER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_process(lines: list[str], returncode: int = 0, delay: float = 0.0) -> MagicMock:
    """Return a mock Popen-like object whose stdout yields the given NDJSON lines."""
    encoded = b"".join((l.rstrip("\n") + "\n").encode() for l in lines)
    buf = io.BytesIO(encoded)

    class _FakeStdout:
        def __init__(self):
            self._buf = buf
            self.fileno = lambda: _make_process._pipe_fd  # real fd set below

        def read(self, n: int = -1):
            if delay:
                time.sleep(delay)
            return self._buf.read(n)

    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.stdout = MagicMock()
    # We'll patch os.read and select.select in tests that need them
    return proc


def _make_pipe_process(lines: list[str], returncode: int = 0) -> subprocess.Popen:
    """Spawn a real subprocess that writes NDJSON lines then exits with returncode."""
    ndjson = "".join(l.rstrip("\n") + "\n" for l in lines)
    script = (
        f"import sys\n"
        f"sys.stdout.write({ndjson!r})\n"
        f"sys.stdout.flush()\n"
        f"sys.exit({returncode})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


class _EchoNormalizer(StreamingDrainerMixin):
    """Minimal adapter that echoes raw chunks as CanonicalEvent(text)."""

    provider_name = "claude"

    def _normalize(self, raw: Dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            dispatch_id=raw.get("dispatch_id", "test-dispatch"),
            terminal_id=raw.get("terminal_id", "T1"),
            provider="claude",
            event_type=raw.get("type", "text"),
            data=raw.get("data", {}),
            observability_tier=2,
        )


class _ErrorNormalizer(StreamingDrainerMixin):
    """Normalizer that always raises to simulate _normalize failures."""

    provider_name = "claude"

    def _normalize(self, raw: Dict[str, Any]) -> CanonicalEvent:
        raise RuntimeError("normalize exploded")


# ---------------------------------------------------------------------------
# Tests: normal stream
# ---------------------------------------------------------------------------

class TestNormalStream:
    def test_events_yielded_in_order(self):
        lines = [
            json.dumps({"type": "init", "data": {"session_id": "s1"}}),
            json.dumps({"type": "text", "data": {"text": "hello"}}),
            json.dumps({"type": "complete", "data": {"exit_code": 0}}),
        ]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        types = [e.event_type for e in events]
        assert types == ["init", "text", "complete"]

    def test_tier_label_overridden_to_1(self):
        lines = [json.dumps({"type": "text", "data": {}})]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert len(events) == 1
        assert events[0].observability_tier == _STREAMING_TIER  # must be 1

    def test_empty_stream_yields_no_events(self):
        proc = _make_pipe_process([], returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))
        assert events == []

    def test_event_store_receives_all_events(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        lines = [
            json.dumps({"type": "text", "data": {"n": i}}) for i in range(5)
        ]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        list(adapter.drain_stream(proc, "T1", "d-001", event_store=es))

        assert es.event_count("T1") == 5

    def test_event_store_uses_explicit_dispatch_id(self, tmp_path):
        from event_store import EventStore

        es = EventStore(events_dir=tmp_path / "events")
        lines = [json.dumps({"type": "text", "data": {}})]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        list(adapter.drain_stream(proc, "T1", "override-dispatch", event_store=es))

        stored = list(es.tail("T1"))
        assert stored[0]["dispatch_id"] == "override-dispatch"


# ---------------------------------------------------------------------------
# Tests: malformed chunks
# ---------------------------------------------------------------------------

class TestMalformedChunks:
    def test_malformed_json_becomes_error_event(self):
        lines = ["this is not json\n"]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert len(events) == 1
        assert events[0].event_type == "error"
        assert "raw" in events[0].data
        assert "this is not json" in events[0].data["raw"]

    def test_json_array_is_error(self):
        lines = [json.dumps([1, 2, 3]) + "\n"]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert len(events) == 1
        assert events[0].event_type == "error"
        assert "expected JSON object" in events[0].data["reason"]

    def test_mixed_valid_and_malformed(self):
        lines = [
            json.dumps({"type": "text", "data": {}}),
            "bad line\n",
            json.dumps({"type": "complete", "data": {}}),
        ]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert len(events) == 3
        assert events[0].event_type == "text"
        assert events[1].event_type == "error"
        assert events[2].event_type == "complete"

    def test_normalize_exception_becomes_error_event(self):
        lines = [json.dumps({"type": "text", "data": {}})]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _ErrorNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert len(events) == 1
        assert events[0].event_type == "error"
        assert "normalize error" in events[0].data["reason"]

    def test_empty_lines_do_not_produce_events(self):
        """Empty lines in stdout are silently skipped (no events, no errors)."""
        # Build a process that outputs blank lines interspersed with valid events
        import subprocess
        script = (
            "import sys\n"
            'sys.stdout.write(\'{"type": "text", "data": {}}\' + "\\n")\n'
            'sys.stdout.write("\\n")\n'  # blank line
            'sys.stdout.write("\\n")\n'  # another blank
            'sys.stdout.write(\'{"type": "complete", "data": {}}\' + "\\n")\n'
            "sys.stdout.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))
        # Only the two valid JSON events; blank lines produce nothing
        types = [e.event_type for e in events]
        assert types == ["text", "complete"]


# ---------------------------------------------------------------------------
# Tests: crash safety (non-zero exit without complete event)
# ---------------------------------------------------------------------------

class TestCrashSafety:
    def test_nonzero_exit_without_complete_emits_synthetic_error(self):
        lines = [json.dumps({"type": "text", "data": {"msg": "partial"}})]
        proc = _make_pipe_process(lines, returncode=1)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        # Last event should be a synthetic error
        assert events[-1].event_type == "error"
        assert "exit" in events[-1].data["reason"].lower() or "code" in events[-1].data["reason"].lower()

    def test_zero_exit_without_complete_no_synthetic_error(self):
        lines = [json.dumps({"type": "text", "data": {}})]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        error_events = [e for e in events if e.event_type == "error"]
        assert error_events == []

    def test_complete_event_suppresses_synthetic_error(self):
        lines = [
            json.dumps({"type": "text", "data": {}}),
            json.dumps({"type": "complete", "data": {}}),
        ]
        proc = _make_pipe_process(lines, returncode=1)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        # complete event seen — no synthetic error should be appended
        error_events = [e for e in events if e.event_type == "error"]
        assert error_events == []


# ---------------------------------------------------------------------------
# Tests: backpressure (bounded queue)
# ---------------------------------------------------------------------------

class TestBackpressure:
    def test_small_queue_does_not_deadlock(self):
        """draining 50 events through a queue of size 4 must not deadlock."""
        lines = [json.dumps({"type": "text", "data": {"n": i}}) for i in range(50)]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()

        events = list(adapter.drain_stream(
            proc, "T1", "d-001", event_store=None, _queue_maxsize=4
        ))
        assert len(events) == 50

    def test_consumer_receives_all_events_with_backpressure(self):
        """Slow consumer (sleep between reads) still gets all events."""
        lines = [json.dumps({"type": "text", "data": {"n": i}}) for i in range(20)]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()

        received = []
        for ev in adapter.drain_stream(proc, "T1", "d-001", event_store=None, _queue_maxsize=2):
            time.sleep(0.001)  # simulate slow consumer
            received.append(ev)

        assert len(received) == 20


# ---------------------------------------------------------------------------
# Tests: tier labeling
# ---------------------------------------------------------------------------

class TestTierLabeling:
    def test_all_events_tier_1(self):
        lines = [
            json.dumps({"type": t, "data": {}})
            for t in ("init", "text", "complete")
        ]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert all(e.observability_tier == 1 for e in events), (
            f"Expected all tier=1, got: {[e.observability_tier for e in events]}"
        )

    def test_error_events_also_tier_1(self):
        lines = ["not-json\n"]
        proc = _make_pipe_process(lines, returncode=0)
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(proc, "T1", "d-001", event_store=None))

        assert events[0].observability_tier == 1


# ---------------------------------------------------------------------------
# Tests: _make_error_event helper
# ---------------------------------------------------------------------------

class TestMakeErrorEvent:
    def test_with_raw(self):
        ev = _make_error_event(
            terminal_id="T1",
            dispatch_id="d-test",
            provider="claude",
            raw="bad chunk",
            reason="parse failed",
        )
        assert ev.event_type == "error"
        assert ev.data["raw"] == "bad chunk"
        assert ev.data["reason"] == "parse failed"

    def test_without_raw(self):
        ev = _make_error_event(
            terminal_id="T1",
            dispatch_id="d-test",
            provider="claude",
            raw=None,
            reason="timeout",
        )
        assert "raw" not in ev.data
        assert ev.data["reason"] == "timeout"

    def test_raw_truncated_at_500(self):
        long_raw = "x" * 1000
        ev = _make_error_event(
            terminal_id="T1",
            dispatch_id="d-test",
            provider="claude",
            raw=long_raw,
            reason="too long",
        )
        assert len(ev.data["raw"]) == 500
