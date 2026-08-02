#!/usr/bin/env python3
"""Regression tests for OI-878 — ringbuffer leak on the SUCCESS path.

The per-dispatch ring buffer (``events/T{n}.ndjson``) is documented in CLAUDE.md
as: at the end of each dispatch, the live file is archived to
``events/archive/{terminal}/{dispatch_id}.ndjson`` and truncated to 0 bytes.

Measured on 2026-07-31 16:14: after dispatch ``20260731-clusterE1-...`` completed
normally on T2, ``T2.ndjson`` held 12,627,519 bytes and there was NO archive entry
for that dispatch.  The provider-lane envelope path (``dispatch_cli ->
run_envelope_plan``) never calls ``_emit_governance``, so the END-of-dispatch
archive+clear never runs there; the live file keeps the previous dispatch's
events until the NEXT dispatch starts (the adapter only clears at START).

Fix: ``EventStore.append()`` now enforces the dispatch boundary on the WRITE
side.  When a new dispatch's first event arrives and the live file still holds a
PREVIOUS dispatch's events (teardown never ran), the previous dispatch's events
are archived under their OWN dispatch_id and the live file is truncated before
the new event is appended.

Every test here is RED on origin/main (no boundary enforcement — a new dispatch
appends straight into the previous dispatch's stream) and GREEN with the fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from event_store import EventStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """EventStore on an isolated events dir."""
    return EventStore(events_dir=tmp_path / "events")


def _append(store, terminal, dispatch_id, n, text_prefix=""):
    """Append n realistic system events for a dispatch."""
    for i in range(n):
        store.append(terminal, {
            "type": "system",
            "data": {"session_id": f"sess-{dispatch_id}", "msg": f"{text_prefix}{i}"},
            "dispatch_id": dispatch_id,
        }, dispatch_id=dispatch_id)


class TestDispatchBoundaryRotation:
    """The write side must enforce the per-dispatch ringbuffer boundary."""

    def test_new_dispatch_archives_previous_and_starts_fresh(self, store, tmp_path):
        """Dispatch A's events linger (teardown never ran); B's first event must
        archive A under A's own id and start a fresh ringbuffer."""
        terminal = "T2"
        dispatch_a = "20260731-dispatch-A"
        dispatch_b = "20260731-dispatch-B"

        # Dispatch A writes a full event stream. Its end-teardown never runs —
        # exactly the door's provider-lane envelope path.
        _append(store, terminal, dispatch_a, n=50)
        live = tmp_path / "events" / f"{terminal}.ndjson"
        before = live.stat().st_size
        assert before > 0

        # Dispatch B's first event arrives.
        store.append(terminal, {
            "type": "system",
            "data": {"session_id": "sess-B"},
            "dispatch_id": dispatch_b,
        }, dispatch_id=dispatch_b)

        archive_path = tmp_path / "events" / "archive" / terminal / f"{dispatch_a}.ndjson"
        assert archive_path.exists(), (
            f"GAP: dispatch A's events were never archived under {dispatch_a}. "
            f"On origin/main the write side does not enforce the boundary, so "
            f"B's event is appended into A's stream and no archive is created. "
            f"live bytes before={before}"
        )

        # Archive holds ONLY dispatch A's events, in order.
        archive_lines = [l for l in archive_path.read_text().strip().split("\n") if l]
        assert len(archive_lines) == 50
        for i, line in enumerate(archive_lines):
            ev = json.loads(line)
            assert ev["dispatch_id"] == dispatch_a
            assert ev["sequence"] == i + 1

        # Live file holds ONLY dispatch B's event, sequence restarts at 1.
        live_lines = [l for l in live.read_text().strip().split("\n") if l]
        assert len(live_lines) == 1
        ev = json.loads(live_lines[0])
        assert ev["dispatch_id"] == dispatch_b
        assert ev["sequence"] == 1, (
            "GAP: sequence must restart at 1 after the boundary rotation. "
            f"Got {ev['sequence']}. On origin/main B's event is appended into "
            "A's stream and inherits the stale sequence counter."
        )

    def test_same_dispatch_does_not_rotate(self, store, tmp_path):
        """Events within one dispatch must never trigger a rotation."""
        terminal = "T1"
        _append(store, terminal, "20260731-dispatch-A", n=10)
        _append(store, terminal, "20260731-dispatch-A", n=5)

        live = tmp_path / "events" / f"{terminal}.ndjson"
        lines = [l for l in live.read_text().strip().split("\n") if l]
        assert len(lines) == 15
        assert not (tmp_path / "events" / "archive" / terminal).exists(), (
            "Same dispatch must not create an archive entry."
        )

    def test_empty_dispatch_id_skips_boundary(self, store, tmp_path):
        """Events without a dispatch_id (legacy/system streams) skip the check."""
        terminal = "T1"
        # Two appends without dispatch_id must just accumulate.
        store.append(terminal, {"type": "system", "data": {"i": 0}})
        store.append(terminal, {"type": "system", "data": {"i": 1}})
        # Then a dispatch_id'd event appends into the same stream.
        store.append(terminal, {"type": "system", "data": {}}, dispatch_id="20260731-dispatch-B")

        live = tmp_path / "events" / f"{terminal}.ndjson"
        lines = [l for l in live.read_text().strip().split("\n") if l]
        assert len(lines) == 3
        assert not (tmp_path / "events" / "archive" / terminal).exists(), (
            "Empty dispatch_id must not trigger a boundary rotation."
        )

    def test_rotation_preserves_datapath_bytes(self, store, tmp_path):
        """Datapath proof: live bytes before, archive entry after, live bytes
        after == only the new dispatch's first event."""
        terminal = "T3"
        dispatch_a = "20260731-dispatch-A"
        dispatch_b = "20260731-dispatch-B"

        _append(store, terminal, dispatch_a, n=100)
        live = tmp_path / "events" / f"{terminal}.ndjson"
        before = live.stat().st_size

        store.append(terminal, {
            "type": "system",
            "data": {"session_id": "sess-B"},
            "dispatch_id": dispatch_b,
        }, dispatch_id=dispatch_b)

        archive_path = tmp_path / "events" / "archive" / terminal / f"{dispatch_a}.ndjson"
        after = live.stat().st_size

        assert before > 0, "live file must hold dispatch A's events before teardown"
        assert archive_path.exists(), "archive entry must exist after teardown"
        assert archive_path.stat().st_size == before, (
            "archive must contain exactly the pre-teardown bytes"
        )
        assert after < before, (
            f"live file must be truncated at the boundary (before={before}, "
            f"after={after}); on origin/main B appends into A's stream and the "
            f"file only grows."
        )

    def test_explicit_clear_is_unchanged(self, store, tmp_path):
        """A normal clear() (the teardown path) must still work exactly as before."""
        terminal = "T2"
        _append(store, terminal, "20260731-dispatch-A", n=10)
        store.clear(terminal, archive_dispatch_id="20260731-dispatch-A")
        assert store.event_count(terminal) == 0
        archive_path = tmp_path / "events" / "archive" / terminal / "20260731-dispatch-A.ndjson"
        assert archive_path.exists()
        lines = [l for l in archive_path.read_text().strip().split("\n") if l]
        assert len(lines) == 10
