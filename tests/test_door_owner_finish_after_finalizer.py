"""OI-2592: ``_owner_finish`` leaves a row the lifecycle finalizer already closed alone.

Under lifecycle v3 the end finalizer projects the dispatch row to its end state
while the lane finishes. The door's ``_owner_finish`` used to walk
accepted -> running -> completed on top of that, hit ``DuplicateTransitionError``
and booked a false ``door_bookkeeping_failed`` per dispatch. These tests run the
real state machine against a real coordination DB: no mock of the transitions.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(REPO / "tests"))

import dispatch_cli  # noqa: E402
from test_create_attempt_project_id import _build_live_shape_db  # noqa: E402

PROJECT = "vnx-dev"


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _build_live_shape_db(state_dir / "runtime_coordination.db")
    return state_dir


def _door_row(state_dir: Path, dispatch_id: str) -> str:
    """Build the row the way the door does, then bring it to ``delivering``."""
    spec = types.SimpleNamespace(
        dispatch_id=dispatch_id, project_id=PROJECT, track_id=None, gate=None,
        target_slot="T1",
    )
    attempt_id = dispatch_cli._persist_dispatch_row(spec, state_dir=state_dir)
    assert attempt_id, "the door could not build the row for the test"
    return attempt_id


def _drive(state_dir: Path, dispatch_id: str, *states: str) -> None:
    for state in states:
        dispatch_cli._owner_transition(dispatch_id, state, state_dir=state_dir)


def _row_state(state_dir: Path, dispatch_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return conn.execute(
            "SELECT state FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _attempt_state(state_dir: Path, attempt_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return conn.execute(
            "SELECT state FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _bookkeeping_failures(state_dir: Path) -> list:
    found = []
    for name in ("dispatch_register.ndjson", "t0_receipts.ndjson"):
        path = state_dir / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and "door_bookkeeping_failed" in line:
                found.append(json.loads(line))
    return found


@pytest.fixture
def delivering(tmp_path):
    state_dir = _state_dir(tmp_path)
    dispatch_id = "20261107-oi2592-owner-finish"
    attempt_id = _door_row(state_dir, dispatch_id)
    _drive(state_dir, dispatch_id, "delivering")
    assert _row_state(state_dir, dispatch_id) == "delivering"
    assert not _bookkeeping_failures(state_dir)
    return state_dir, dispatch_id, attempt_id


def test_success_after_finalizer_completed_the_row(delivering):
    state_dir, dispatch_id, attempt_id = delivering
    _drive(state_dir, dispatch_id, "accepted", "running", "completed")

    dispatch_cli._owner_finish(dispatch_id, attempt_id, state_dir=state_dir, success=True)

    assert _bookkeeping_failures(state_dir) == []
    assert _row_state(state_dir, dispatch_id) == "completed"
    assert _attempt_state(state_dir, attempt_id) == "succeeded"


def test_failure_after_finalizer_closed_the_row(delivering):
    state_dir, dispatch_id, attempt_id = delivering
    _drive(state_dir, dispatch_id, "failed_delivery")

    dispatch_cli._owner_finish(
        dispatch_id, attempt_id, state_dir=state_dir, success=False,
        failure_reason="lane failed",
    )

    assert _bookkeeping_failures(state_dir) == []
    assert _row_state(state_dir, dispatch_id) == "failed_delivery"
    assert _attempt_state(state_dir, attempt_id) == "failed"


def test_timed_out_row_is_left_alone(delivering):
    state_dir, dispatch_id, attempt_id = delivering
    _drive(state_dir, dispatch_id, "timed_out")

    dispatch_cli._owner_finish(
        dispatch_id, attempt_id, state_dir=state_dir, success=False,
        failure_reason="timed out",
    )

    assert _bookkeeping_failures(state_dir) == []
    assert _row_state(state_dir, dispatch_id) == "timed_out"
    assert _attempt_state(state_dir, attempt_id) == "failed"


def test_row_not_yet_closed_still_walks_the_hops(delivering):
    state_dir, dispatch_id, attempt_id = delivering

    dispatch_cli._owner_finish(dispatch_id, attempt_id, state_dir=state_dir, success=True)

    assert _bookkeeping_failures(state_dir) == []
    assert _row_state(state_dir, dispatch_id) == "completed"
    assert _attempt_state(state_dir, attempt_id) == "succeeded"


def test_not_yet_closed_failure_ends_failed_delivery(delivering):
    state_dir, dispatch_id, attempt_id = delivering

    dispatch_cli._owner_finish(
        dispatch_id, attempt_id, state_dir=state_dir, success=False,
        failure_reason="lane failed",
    )

    assert _row_state(state_dir, dispatch_id) == "failed_delivery"
    assert _attempt_state(state_dir, attempt_id) == "failed"


def test_absent_row_is_still_one_bookkeeping_failure_per_hop_site(tmp_path):
    state_dir = _state_dir(tmp_path)

    dispatch_cli._owner_finish("20261107-oi2592-absent", None, state_dir=state_dir, success=True)

    sites = [f.get("extra", {}).get("site") or f.get("site") for f in _bookkeeping_failures(state_dir)]
    assert sites, "a genuinely broken transition must still be booked"
    assert all(s.startswith("_owner_transition:") for s in sites)


def test_unreadable_store_falls_back_to_walking_the_hops(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "runtime_coordination.db").write_bytes(b"not a database")

    assert dispatch_cli._owner_row_closed_by_finalizer("x", state_dir=state_dir) is False
