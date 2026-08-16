#!/usr/bin/env python3
"""Regression tests for OI-878 / OI-902 — envelope-path end-of-dispatch teardown.

The per-dispatch ring buffer (``events/T{n}.ndjson``) is documented as: at the
end of each dispatch, the live file is archived to
``events/archive/{terminal}/{dispatch_id}.ndjson`` and truncated to 0 bytes.

Measured 2026-08-01 (Vincent): after six deepseek-harness dispatches on code
with #1276, the dispatcher log reported ``event_store: dispatch boundary
20260801-w1-drain-thread-leak -> 20260801-w2-vnxmode-resolver — archived
previous dispatch events and cleared live file (end-teardown had not run)``.

The rotation visible in the archives happens on the WRITE side — the boundary
guard of the NEXT dispatch (EventStore.append, #1276).  The END-teardown of the
closing dispatch never runs, because the door's provider-lane envelope path
(``dispatch_cli -> run_envelope_plan -> ProviderAdapter -> spawn_*``) never
called the archive/clear that ``provider_dispatch._emit_governance`` performs
on the side-door path.  A subsequent dispatch therefore rotates the previous
stream, and the LAST dispatch in a series leaks its events into the live file
forever.

Fix: ``dispatch_envelope._govern`` now archives the live stream under the
dispatch's own id BEFORE writing the receipt (so the receipt carries
``events_path``) and clears the live file AFTER the receipt, in a finally so the
clear survives a receipt-emit failure (a proof-chain gap, OI-1179).  The
boundary guard of #1276 stays as the second line of defense.

Every test here is RED on origin/main (no end-teardown in the envelope path)
and GREEN with the fix.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_envelope import (  # noqa: E402
    EnvelopeSpec,
    ProviderAdapter,
    _AdapterResult,
    _govern,
    run_envelope_plan,
)
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_spec import Provider  # noqa: E402
from event_store import EventStore  # noqa: E402

# Reuse the plan factory from test_dispatch_envelope_plan without importing it
# (importing the test module would re-run its module-level side effects). The
# factory is small; duplicate it here so this file is self-contained.
from test_dispatch_envelope_plan import _make_provider_plan  # noqa: E402

_FAKE_WT_PATH = Path("/tmp/fake-worktrees/oi902-teardown-test")


def _write_events(events_dir: Path, terminal: str, dispatch_id: str, n: int = 10) -> None:
    """Write n events to the live file, exactly as SubprocessAdapter.append does."""
    store = EventStore(events_dir=events_dir)
    for i in range(n):
        store.append(terminal, {"type": "system", "data": {"i": i}}, dispatch_id=dispatch_id)


def _make_spec(state_dir: Path, data_dir: Path, dispatch_id: str, terminal: str = "T2") -> EnvelopeSpec:
    return EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id=terminal,
        provider="deepseek-harness",
        model="deepseek-v4-flash",
        instruction="end-teardown regression dispatch",
        role=None,
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


@pytest.fixture
def dirs(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    events_dir = tmp_path / "events"
    state_dir.mkdir()
    data_dir.mkdir()
    return state_dir, data_dir, events_dir


class TestGovernEndTeardown:
    """_govern (the envelope governance chokepoint) must archive + clear the live
    event stream under the dispatch's OWN id, so the LAST dispatch in a series is
    cleaned up too."""

    def test_govern_archives_and_clears_on_success(self, dirs):
        """The core OI-902 assertion: after a dispatch completes through the
        envelope path, an archive file with the dispatch-id exists, the live file
        is truncated, and the receipt carries events_path. On origin/main no
        archive is created and the live file keeps the events."""
        state_dir, data_dir, events_dir = dirs
        dispatch_id = "oi902-govern-success"

        with patch("event_store._events_dir", return_value=events_dir):
            _write_events(events_dir, "T2", dispatch_id, n=25)

            spec = _make_spec(state_dir, data_dir, dispatch_id)
            start = end = datetime.now(timezone.utc)
            _report, receipt_path = _govern(
                spec, _AdapterResult(returncode=0, completion_text="done", status="success"), start, end,
            )

        live = events_dir / "T2.ndjson"
        archive = events_dir / "archive" / "T2" / f"{dispatch_id}.ndjson"
        assert archive.exists(), (
            f"GAP: no archive entry for {dispatch_id}. On origin/main the envelope "
            "path never runs the end-teardown, so the stream stays in the live file "
            "until the NEXT dispatch's boundary guard rotates it."
        )
        assert archive.stat().st_size > 0
        assert not live.exists() or live.stat().st_size == 0, (
            "GAP: live file must be truncated by the dispatch's own teardown, not "
            "left for the next dispatch."
        )

        # Receipt carries the events_path pointer (parity with _emit_governance).
        assert receipt_path is not None and receipt_path.exists()
        receipt_text = receipt_path.read_text(encoding="utf-8")
        assert str(archive) in receipt_text, (
            f"receipt must point at the archived stream; got: {receipt_text[:400]}"
        )

    def test_govern_archives_and_clears_on_failure(self, dirs):
        """Failure/timeout outcomes must also tear the stream down."""
        state_dir, data_dir, events_dir = dirs
        dispatch_id = "oi902-govern-failure"

        with patch("event_store._events_dir", return_value=events_dir):
            _write_events(events_dir, "T2", dispatch_id, n=8)
            spec = _make_spec(state_dir, data_dir, dispatch_id)
            start = end = datetime.now(timezone.utc)
            _govern(
                spec,
                _AdapterResult(returncode=1, completion_text="", status="failure", error="boom"),
                start, end,
            )

        archive = events_dir / "archive" / "T2" / f"{dispatch_id}.ndjson"
        live = events_dir / "T2.ndjson"
        assert archive.exists() and archive.stat().st_size > 0
        assert not live.exists() or live.stat().st_size == 0

    def test_govern_clears_even_when_receipt_raise(self, dirs):
        """The clear runs in finally: a receipt-emit failure must not leave
        the live file holding the dispatch's events.

        OI-1179: the failed receipt emit is a proof-chain gap, not a raise —
        ``_govern`` records it and returns ``receipt_path=None`` so the WORK
        status is preserved. The end-teardown still runs.
        """
        state_dir, data_dir, events_dir = dirs
        dispatch_id = "oi902-govern-receipt-raise"

        with patch("event_store._events_dir", return_value=events_dir):
            _write_events(events_dir, "T2", dispatch_id, n=5)
            spec = _make_spec(state_dir, data_dir, dispatch_id)
            start = end = datetime.now(timezone.utc)
            with patch(
                "governance_emit.emit_dispatch_receipt",
                side_effect=RuntimeError("receipt disk failure"),
            ), patch("governance_emit.emit_unified_report", return_value=Path("/tmp/fake-report.md")):
                _report, receipt_path = _govern(
                    spec, _AdapterResult(returncode=0, completion_text="done", status="success"), start, end,
                )

        # OI-1179: no raise — the gap is recorded, the receipt path is None.
        assert receipt_path is None

        live = events_dir / "T2.ndjson"
        archive = events_dir / "archive" / "T2" / f"{dispatch_id}.ndjson"
        # Archive happened before the receipt emit (top of _govern).
        assert archive.exists() and archive.stat().st_size > 0
        # Clear still ran in finally despite the failed receipt write.
        assert not live.exists() or live.stat().st_size == 0

    def test_govern_does_not_mislabel_previous_dispatch(self, dirs):
        """Guard: when the live file holds a DIFFERENT dispatch's events (this
        dispatch never wrote — e.g. pre-spawn failure), _govern must NOT archive
        them under our id, and must NOT clear them away. Those events belong to
        the boundary guard of the NEXT dispatch."""
        state_dir, data_dir, events_dir = dirs
        prev_dispatch = "oi902-prev-dispatch"
        current_dispatch = "oi902-current-no-events"

        with patch("event_store._events_dir", return_value=events_dir):
            _write_events(events_dir, "T2", prev_dispatch, n=12)
            spec = _make_spec(state_dir, data_dir, current_dispatch)
            start = end = datetime.now(timezone.utc)
            _govern(
                spec,
                _AdapterResult(returncode=1, completion_text="", status="failure", error="pre-spawn"),
                start, end,
            )

        archive_current = events_dir / "archive" / "T2" / f"{current_dispatch}.ndjson"
        assert not archive_current.exists(), (
            "GAP: previous dispatch's events must never be archived under the "
            "current dispatch's id."
        )
        live = events_dir / "T2.ndjson"
        assert live.exists() and live.stat().st_size > 0, (
            "GAP: a different dispatch's stream must not be cleared by our teardown."
        )


class TestEnvelopePlanEndTeardown:
    """End-to-end through run_envelope_plan — the exact door path that leaked."""

    def test_run_envelope_plan_archives_and_clears(self, dirs):
        """A deepseek-harness dispatch through run_envelope_plan (the door's
        provider lane) must archive the live stream under its own id and truncate
        it — WITHOUT a next dispatch to trigger the boundary guard."""
        state_dir, data_dir, events_dir = dirs
        dispatch_id = "oi902-plan-teardown"
        plan = _make_provider_plan(
            tmp_path := events_dir.parent,
            provider=Provider.DEEPSEEK_HARNESS,
            dispatch_id=dispatch_id,
            target_id="T2",
        )
        permit = issue_permit(plan)
        _fake_consumer_root = events_dir.parent / "consumer-root"

        def fake_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None, role=None):
            # The SubprocessAdapter writes events internally for the harness lane.
            _write_events(events_dir, plan_arg.target_id, plan_arg.dispatch_id, n=15)
            return _AdapterResult(returncode=0, completion_text="done", status="success")

        with patch("event_store._events_dir", return_value=events_dir), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=_FAKE_WT_PATH), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(ProviderAdapter, "run", fake_adapter_run):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "success"
        live = events_dir / "T2.ndjson"
        archive = events_dir / "archive" / "T2" / f"{dispatch_id}.ndjson"
        assert archive.exists(), (
            "GAP: run_envelope_plan (the door's deepseek-harness lane) produced "
            "no archive entry — the end-teardown never ran."
        )
        assert archive.stat().st_size > 0
        assert not live.exists() or live.stat().st_size == 0, (
            "GAP: live file must be truncated by the dispatch's own end-teardown; "
            "the LAST dispatch in a series has no successor to trigger the boundary guard."
        )
