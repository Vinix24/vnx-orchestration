"""OI-1814 / OI-2482: a ``--refire`` of a failed dispatch walks the row through ``recovered``.

Dispatch-ID: dlv-loop-refire-recovered

Before this, the door's claim met a ``failed_delivery``/``timed_out`` row, was refused as an
idempotent no-op, and every owner hop after it was refused and booked as
``door_bookkeeping_failed``. The second attempt ran and landed; the row stayed on the failure
of the first, which the lifecycle count reads as a projection conflict. Every store here is a
real ``runtime_coordination.db`` in ``tmp_path`` in the measured live shape, driven by the
door's own functions; the start and end lines and the count come from the K-9 test module,
never a copy.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import dispatch_cli
import test_k9_lifecycle_count as k9
from coordination_db import get_connection
from test_create_attempt_project_id import _build_live_shape_db

# The K-9 line writers and their pins, re-bound so pytest finds them in this module.
_hermetic_append = k9._hermetic_append
git_repo = k9.git_repo
writer_sha = k9.writer_sha
lifecycle_on = k9.lifecycle_on

REASON = "OI-1814 test refire"
REFIRE_WALK = ["failed_delivery", "recovered", "claimed", "delivering", "accepted", "running"]


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """The live-shape store the door writes to (tests/test_create_attempt_project_id.py)."""
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    _build_live_shape_db(sd / "runtime_coordination.db")
    return sd


def _spec(dispatch_id: str):
    return types.SimpleNamespace(dispatch_id=dispatch_id, project_id=k9.PROJECT, track_id=None,
                                 gate=None, target_slot="T1")


def _row(state_dir: Path, dispatch_id: str) -> dict:
    with get_connection(state_dir) as conn:
        return dict(conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ?",
                                 (dispatch_id,)).fetchone())


def _walk(state_dir: Path, dispatch_id: str) -> list:
    """Every real transition of the row, in order, as ``(from_state, to_state)``."""
    with get_connection(state_dir) as conn:
        events = conn.execute(
            "SELECT event_type, from_state, to_state FROM coordination_events "
            "WHERE entity_type = 'dispatch' AND entity_id = ? ORDER BY id", (dispatch_id,)).fetchall()
    return [(e["from_state"], e["to_state"]) for e in events
            if e["to_state"] and e["event_type"] == f"dispatch_{e['to_state']}"]


def _bookkeeping_failures(state_dir: Path) -> list:
    found = []
    for name in ("dispatch_register.ndjson", "t0_receipts.ndjson"):
        path = state_dir / name
        if path.exists():
            found += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                      if "door_bookkeeping_failed" in line]
    return found


def _first_attempt(state_dir: Path, dispatch_id: str, end: str) -> None:
    """Attempt 1 the way the door runs it, ending in ``end``."""
    attempt = dispatch_cli._persist_dispatch_row(_spec(dispatch_id), state_dir=state_dir)
    assert attempt, "the door could not build the row for the test"
    dispatch_cli._owner_transition(dispatch_id, "delivering", state_dir=state_dir)
    if end == "timed_out":
        dispatch_cli._owner_transition(dispatch_id, "timed_out", state_dir=state_dir)
    dispatch_cli._owner_finish(dispatch_id, attempt, state_dir=state_dir,
                               success=end == "completed", failure_reason="attempt 1 failed")
    if end in ("expired", "dead_letter"):
        dispatch_cli._owner_transition(dispatch_id, end, state_dir=state_dir)
    assert _row(state_dir, dispatch_id)["state"] == end


def _refire(state_dir: Path, dispatch_id: str, *, success: bool, reason=REASON):
    attempt = dispatch_cli._persist_dispatch_row(_spec(dispatch_id), state_dir=state_dir,
                                                 refire_reason=reason)
    dispatch_cli._owner_transition(dispatch_id, "delivering", state_dir=state_dir)
    dispatch_cli._owner_finish(dispatch_id, attempt, state_dir=state_dir, success=success,
                               failure_reason=None if success else "attempt 2 failed")
    return attempt


@pytest.mark.parametrize("failure", ["failed_delivery", "timed_out"])
def test_refire_of_a_failed_row_ends_completed_at_attempt_2(state_dir, failure):
    """Tests 1 and 2: the OI-1814 acceptance, from ``failed_delivery`` and from ``timed_out``."""
    dispatch_id = f"dlv-refire-{failure}"
    _first_attempt(state_dir, dispatch_id, failure)

    attempt = _refire(state_dir, dispatch_id, success=True)

    row = _row(state_dir, dispatch_id)
    assert attempt is not None
    assert (row["state"], row["attempt_count"]) == ("completed", 2)
    walk = _walk(state_dir, dispatch_id)
    states = [failure, *REFIRE_WALK[1:], "completed"]
    assert walk[-6:] == list(zip(states, states[1:]))
    assert all(prev[1] == nxt[0] for prev, nxt in zip(walk, walk[1:])), walk
    assert _bookkeeping_failures(state_dir) == []


def test_refire_whose_second_attempt_fails_ends_failed_delivery_at_attempt_2(state_dir):
    """Test 3: not attempt 1, and not left on ``recovered``."""
    dispatch_id = "dlv-refire-fails-again"
    _first_attempt(state_dir, dispatch_id, "failed_delivery")

    _refire(state_dir, dispatch_id, success=False)

    row = _row(state_dir, dispatch_id)
    assert (row["state"], row["attempt_count"]) == ("failed_delivery", 2)
    assert [to for _, to in _walk(state_dir, dispatch_id)][-6:] == [*REFIRE_WALK[1:], "failed_delivery"]
    assert _bookkeeping_failures(state_dir) == []


def test_lifecycle_count_sees_no_projection_conflict_after_a_refire(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Test 4: v3 start and end lines for attempt 1 (failed) and attempt 2 (completed)."""
    dispatch_id = "dlv-refire-count"
    spec = _spec(dispatch_id)
    first = dispatch_cli._persist_dispatch_row(spec, state_dir=state_dir)
    dispatch_cli._owner_transition(dispatch_id, "delivering", state_dir=state_dir)
    k9._finish(state_dir, k9._begin(state_dir, dispatch_id), end_state="failed")
    dispatch_cli._owner_finish(dispatch_id, first, state_dir=state_dir, success=False)
    assert _row(state_dir, dispatch_id)["state"] == "failed_delivery"

    second = dispatch_cli._persist_dispatch_row(spec, state_dir=state_dir, refire_reason=REASON)
    dispatch_cli._owner_transition(dispatch_id, "delivering", state_dir=state_dir)
    handover = k9._begin(state_dir, dispatch_id)
    k9._finish(state_dir, handover, end_state="completed")
    dispatch_cli._owner_finish(dispatch_id, second, state_dir=state_dir, success=True)

    assert handover.attempt == 2
    count = k9._count(state_dir, git_repo)
    assert count["status"] == "measured", count
    assert count["counts"]["projection_conflict"] == 0, count["findings"]
    row = _row(state_dir, dispatch_id)
    assert (row["state"], row["attempt_count"]) == ("completed", 2)
    assert _bookkeeping_failures(state_dir) == []


@pytest.mark.parametrize("terminal", ["expired", "completed", "dead_letter"])
def test_refire_of_a_terminal_row_leaves_the_row_and_books_nothing(state_dir, terminal, capsys):
    """Test 5: the row is unchanged and no fact names a transition out of a terminal state."""
    dispatch_id = f"dlv-refire-{terminal}"
    _first_attempt(state_dir, dispatch_id, terminal)
    before = _row(state_dir, dispatch_id)

    attempt = _refire(state_dir, dispatch_id, success=True)

    assert attempt is None
    assert _row(state_dir, dispatch_id) == before
    assert _bookkeeping_failures(state_dir) == []
    assert f"is terminal ({terminal})" in capsys.readouterr().err


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_no_refire_reason_never_walks_a_failed_row(state_dir, reason):
    """Fail closed: only an explicit ``--refire`` reason moves a failed row to ``recovered``."""
    dispatch_id = "dlv-refire-no-reason"
    _first_attempt(state_dir, dispatch_id, "failed_delivery")
    before = _row(state_dir, dispatch_id)

    attempt = dispatch_cli._persist_dispatch_row(_spec(dispatch_id), state_dir=state_dir,
                                                 refire_reason=reason)

    assert attempt is None
    assert _row(state_dir, dispatch_id) == before


def test_refire_of_a_row_in_flight_is_unchanged(state_dir):
    """A retry while the row is still ``delivering`` neither recovers nor claims it."""
    dispatch_id = "dlv-refire-in-flight"
    dispatch_cli._persist_dispatch_row(_spec(dispatch_id), state_dir=state_dir)
    dispatch_cli._owner_transition(dispatch_id, "delivering", state_dir=state_dir)
    before = _row(state_dir, dispatch_id)

    attempt = dispatch_cli._persist_dispatch_row(_spec(dispatch_id), state_dir=state_dir,
                                                 refire_reason=REASON)

    assert attempt is None
    assert _row(state_dir, dispatch_id) == before
