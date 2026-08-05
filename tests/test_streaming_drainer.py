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
    _run_producer,
    _STREAMING_TIER,
    coerce_chunk_stall,
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
# Tests: OI-903 chunk-stall / total-deadline relationship
# ---------------------------------------------------------------------------

class TestChunkStallCoercion:
    """The chunk (stall) timeout must scale with the total deadline."""

    def test_kimi_long_deadline_stall_floor(self):
        """1200s stall default with a 3600s deadline is floored to 1800s.

        Regression for OI-903: a kimi worker was SIGTERM'd at 1890.6s by the
        1200s chunk timeout after a legitimate 1200s silent thinking phase.
        """
        assert coerce_chunk_stall(1200.0, 3600.0) == 1800.0

    def test_chunk_never_exceeds_total_deadline(self):
        """A stall timeout above the deadline is capped at the deadline."""
        assert coerce_chunk_stall(4000.0, 3600.0) == 3600.0
        assert coerce_chunk_stall(300.0, 900.0) == 450.0

    def test_positive_deadline_floor_unchanged_when_already_above(self):
        """A chunk already above half the deadline passes through unchanged."""
        assert coerce_chunk_stall(1800.0, 3600.0) == 1800.0

    def test_nonpositive_values_unchanged(self):
        """Non-positive deadline or chunk is left alone (loop fires immediately)."""
        assert coerce_chunk_stall(1200.0, 0.0) == 1200.0
        assert coerce_chunk_stall(0.0, 3600.0) == 0.0
        assert coerce_chunk_stall(1200.0, -5.0) == 1200.0


class TestStallSurvival:
    """A worker silent longer than the raw chunk timeout but within its deadline
    must NOT be killed by the stall detector (OI-903)."""

    def test_long_silence_within_deadline_not_killed(self, monkeypatch):
        """chunk_timeout=1 with a 10s deadline: the floor raises the effective
        stall to 5s, so a 2s silent gap between lines survives.

        Red on the old code: the 1s chunk timeout fires at t=1s and the worker
        is killed before its second line (t=2s) arrives.
        """
        monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)

        script = (
            "import sys, time\n"
            'sys.stdout.write(\'{"type":"text","data":{"n":0}}\' + "\\n")\n'
            "sys.stdout.flush()\n"
            "time.sleep(2)\n"
            'sys.stdout.write(\'{"type":"text","data":{"n":1}}\' + "\\n")\n'
            "sys.stdout.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # own session so _kill_process's killpg is scoped
        )
        adapter = _EchoNormalizer()
        events = list(adapter.drain_stream(
            proc, "T1", "d-oi903", event_store=None,
            chunk_timeout=1.0, total_deadline=10.0,
        ))

        types = [e.event_type for e in events]
        assert types == ["text", "text"], (
            f"expected both lines delivered, got {types} (worker was killed by "
            f"an un-scaled chunk timeout)"
        )
        assert not [e for e in events if e.event_type == "error"]

    def test_env_chunk_timeout_override_skips_floor(self, monkeypatch):
        """VNX_CHUNK_TIMEOUT retains top precedence: an explicit override fires
        fast even when far below the deadline (no floor applied)."""
        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.2")
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)

        script = (
            "import sys, time\n"
            "sys.stdout.write('hello-not-json')\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # own session so _kill_process's killpg is scoped
        )
        adapter = _EchoNormalizer()
        t0 = time.monotonic()
        events = list(adapter.drain_stream(
            proc, "T1", "d-env", event_store=None,
            chunk_timeout=30.0, total_deadline=30.0,
        ))
        elapsed = time.monotonic() - t0

        assert elapsed < 3.0, f"env override should fire fast, got {elapsed:.2f}s"
        assert any(e.event_type == "error" for e in events)


class TestDeadlineCompressedStallLabeling:
    """OI-1044 co-death investigation: when total_deadline is close, `remaining`
    (the actual select() wait) is compressed below the configured chunk_timeout.
    A `not ready` in that compressed window must never be reported as
    "chunk timeout {chunk_timeout}s exceeded" — that overstates how long the
    worker was actually silent and mislabels a deadline-driven kill as a
    silence-driven one, which is exactly what made a kimi dispatch look like
    it "died right after finishing a tool call" instead of "ran out of its
    total budget".

    Tests call ``_run_producer`` directly (not the ``drain_stream`` wrapper)
    with post-coercion values: in a real long-running dispatch, `chunk_timeout`
    is already floored to `<= total_deadline` by `coerce_chunk_stall` at drain
    start, and the compression this class targets only appears once `elapsed`
    has grown close to `total_deadline` over the dispatch's lifetime — calling
    `_run_producer` directly lets the test represent "we are already near the
    deadline" without an actual multi-minute wait.
    """

    @staticmethod
    def _run_and_collect(*, chunk_timeout: float, total_deadline: float, script: str):
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        result_queue: queue.Queue = queue.Queue()
        try:
            _run_producer(
                process=proc,
                terminal_id="T1",
                dispatch_id="d-labeling",
                event_store=None,
                chunk_timeout=chunk_timeout,
                total_deadline=total_deadline,
                result_queue=result_queue,
                seen_complete=threading.Event(),
                timed_out=threading.Event(),
                normalize_fn=lambda raw: CanonicalEvent(
                    dispatch_id="d-labeling", terminal_id="T1", provider="claude",
                    event_type=raw.get("type", "text"), data=raw.get("data", {}),
                    observability_tier=2,
                ),
                provider_name="claude",
            )
        finally:
            proc.wait(timeout=5)
        events = []
        while not result_queue.empty():
            events.append(result_queue.get_nowait())
        return events

    def test_kill_near_deadline_reports_actual_wait_not_full_chunk_timeout(self):
        """chunk_timeout=5.0 with only 1.0s of total budget left: the select()
        wait is compressed to ~1.0s, not the full 5.0s chunk window. The
        synthetic error's reason must reflect the ~1s actual gap, not falsely
        claim a full 5s chunk_timeout elapsed."""
        events = self._run_and_collect(
            chunk_timeout=5.0, total_deadline=1.0,
            script="import time\ntime.sleep(3)\n",  # never writes anything
        )

        errors = [e for e in events if e.event_type == "error"]
        assert len(errors) == 1
        reason = errors[0].data.get("reason", "")
        assert reason != "chunk timeout 5s exceeded", (
            f"deadline-compressed kill must not claim the full chunk_timeout elapsed: {reason!r}"
        )
        assert "deadline" in reason.lower()
        assert "1s" in reason

    def test_kill_with_no_deadline_pressure_keeps_chunk_timeout_label(self):
        """chunk_timeout=1.0 with a roomy total_deadline=30.0: remaining ==
        chunk_timeout (not compressed), so the ORIGINAL "chunk timeout Ns
        exceeded" label is still correct and must be unchanged."""
        events = self._run_and_collect(
            chunk_timeout=1.0, total_deadline=30.0,
            script="import time\ntime.sleep(3)\n",
        )

        errors = [e for e in events if e.event_type == "error"]
        assert len(errors) == 1
        reason = errors[0].data.get("reason", "")
        assert reason == "chunk timeout 1s exceeded"


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
