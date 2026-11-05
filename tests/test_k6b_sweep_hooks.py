"""ADR-045 audit-spoor-kern K-6b: the sweep also runs at the door and in the build.

Dispatch-ID: dlv-2892f07f7726

K-6 wired the sweep into the runner tick only, and the deadline lookup could
not see a spec staged under ``pending/<staging_id>/`` when the staging id
differs from the dispatch id (OI-2522): a valid 300-second deadline fell back
to 14400. This file covers plan row 6 and OI-2522:

1. the start line carries the spec's own ``deadline_seconds`` and
   ``deadline_for`` reads it first (source ``start_line``);
2. a spec without a valid integer leaves the field out and the old lookup
   order still holds;
3. a spent budget stops the sweep between items, never inside one, and the
   next sweep finishes the rest;
4. the door sweeps once, bounded at 2 s, before the hand-over, and a raising
   sweep never refuses or changes the fire's exit code (nor does ``--dry-run``
   sweep at all);
5. ``build_t0_state`` sweeps once per build, reports it in the state, and a
   raising sweep leaves the exit code unchanged;
6. two overlapping sweeps on one dead attempt still give one end line;
7. a bounded sweep that stops early leaves a continuation cursor, so repeated
   bounded sweeps visit every attempt instead of restarting on the live prefix
   (codex finding on the round-1 head);
8. a cursor is read back only with a ``project_id``, a ``dispatch_id``, an
   integer ``attempt`` of at least 1 and a non-negative integer ``offset``;
   anything else — a cursor left without a ``project_id`` included — is the
   front for the next bounded sweep and never an error inside ``_resume_at``
   (codex finding on the round-2 head);
9. a bounded sweep walks ONE sequence — the open intents then the in-flight
   attempts — starting at the cursor's item and wrapping, so a failing intent
   costs one item of one run and never every run (OI-2527, OI-2530); the cursor
   carries its item's ``kind`` and combined offset, and a K-6b cursor without a
   ``kind`` resumes at its attempt.

Every store is real (a real ledger, a real ``runtime_coordination.db``) under
``tmp_path``; only clocks and process liveness are pinned.
"""

from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPT_DIR / "lib"
for _p in (str(LIB_DIR), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402
import coordination_db  # noqa: E402
import dispatch_cli  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
    prepare_end_line,
    write_end_intent,
)
from append_receipt_internals.payload import append_receipt_payload  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402
from coordination_db import get_connection  # noqa: E402
from dispatch_plan import ModelPin, RuntimeSnapshot  # noqa: E402

PROJECT = "vnx-dev"  # the store's derived authority for a tmp bundle (as in K-4b)
DISPATCH = "d-k6b"
ATTEMPT = 1
ROLE = "backend-developer"
DEADLINE = 300  # dispatch_spec.DEADLINE_SECONDS_MIN: the smallest legal deadline
GRACE = 30 * 60  # the 30-minute decision of 15-10, pinned literally
START = "2026-11-01T10:00:00Z"
START_DT = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
_STALE_DOOR_START = "Mon Jan  1 00:00:00 2024"

_RUNNER_PATH_0017 = LIB_DIR / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k6b", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


# ---------------------------------------------------------------------------
# Fixtures / helpers (real stores, real lines)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the real ledger layer, never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "vnx-data"


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = data_dir / "state"
    sd.mkdir(parents=True)
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _finished(state_dir: Path, dispatch_id: str = DISPATCH, attempt: int = ATTEMPT) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == "dispatch_finished"
            and line.get("dispatch_id") == dispatch_id and line.get("attempt") == attempt]


def _starts(state_dir: Path, dispatch_id: str = DISPATCH) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == "dispatch_started"
            and line.get("dispatch_id") == dispatch_id]


def _append(state_dir: Path, line: dict):
    return append_receipt_payload(dict(line), receipts_file=str(_ledger(state_dir)),
                                  skip_enrichment=True)


def _spec(state_dir: Path, *, dispatch_id: str = DISPATCH, deadline: int = DEADLINE,
          subdir: str = "completed") -> "tuple[Path, str]":
    """A staged ``dispatch-spec.json`` and the sha256 a start line would record."""
    path = state_dir.parent / "dispatches" / subdir / dispatch_id / "dispatch-spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"dispatch_id": dispatch_id, "deadline_seconds": deadline})
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_spec(data_dir: Path, *, dispatch_id: str, staging_id: str, deadline: object,
                 include: bool = True) -> Path:
    """A staging bundle; ``staging_id`` may differ from ``dispatch_id`` (OI-2522)."""
    bundle = data_dir / "dispatches" / "pending" / staging_id
    bundle.mkdir(parents=True, exist_ok=True)
    payload = {"dispatch_id": dispatch_id, "staging_id": staging_id}
    if include:
        payload["deadline_seconds"] = deadline
    path = bundle / "dispatch-spec.json"
    atomic_write_json(path, payload)
    return path


def _start_line(state_dir: Path, *, project_id: str = PROJECT, dispatch_id: str = DISPATCH,
                attempt: int = ATTEMPT, mode: str = "v3",
                spec_sha: str = "0" * 64, timestamp: str = START,
                door_pid: int = 0, door_started_at: str = _STALE_DOOR_START) -> dict:
    line = {
        "timestamp": timestamp, "event_type": "dispatch_started",
        "project_id": project_id, "dispatch_id": dispatch_id, "attempt": attempt,
        "role": ROLE, "base_sha": "a" * 40, "spec_sha256": spec_sha,
        "lifecycle_mode": mode, "door_pid": door_pid,
        "door_started_at": door_started_at, "requested_by": "terminal:T1",
    }
    _append(state_dir, line)
    return line


def _insert_row(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                state: str = "running", attempt_count: int = ATTEMPT) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count,
             json.dumps({"lifecycle_mode": "v3"})),
        )
        conn.commit()


def _now_after(start: datetime, seconds: float) -> datetime:
    return start + timedelta(seconds=seconds)


def _heartbeat(state_dir: Path) -> dict:
    path = state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _begin(state_dir: Path, spec_file: Path, *, dispatch_id: str = DISPATCH,
           project_id: str = PROJECT) -> dispatch_start.Handover:
    """A real ``dispatch_start.begin`` line; only the clock and door identity are pinned."""
    with patch.object(dispatch_start, "process_started_at", lambda pid: _STALE_DOOR_START), \
            patch.object(dispatch_start, "_now", lambda: START):
        return dispatch_start.begin(project_id=project_id, dispatch_id=dispatch_id,
                                    role=ROLE, base_sha="a" * 40, spec_file=spec_file,
                                    target_slot="T1", state_dir=state_dir)


def _seed_sweepable(state_dir: Path, *, dispatch_id: str = DISPATCH) -> None:
    """One dead v3 attempt whose spec carries the smallest legal deadline."""
    _spec_path, spec_sha = _spec(state_dir, dispatch_id=dispatch_id)
    _insert_row(state_dir, dispatch_id=dispatch_id)
    _start_line(state_dir, dispatch_id=dispatch_id, spec_sha=spec_sha)


def _attempt(state_dir: Path, dispatch_id: str, *, live: bool) -> None:
    """One in-flight v3 attempt: live through this test's own pid, or dead.

    A dead attempt carries a spec with the smallest legal deadline, so the
    sweep abandons it; a live one is never past anything (liveness comes first).
    """
    spec_sha = "0" * 64
    if live:
        pid, door_started_at = os.getpid(), dispatch_start.process_started_at(os.getpid())
        assert door_started_at, "ps reported no start time for this test's own pid"
    else:
        pid, door_started_at = 0, _STALE_DOOR_START
        _spec_path, spec_sha = _spec(state_dir, dispatch_id=dispatch_id)
    _insert_row(state_dir, dispatch_id=dispatch_id)
    _start_line(state_dir, dispatch_id=dispatch_id, spec_sha=spec_sha,
                door_pid=pid, door_started_at=door_started_at)


def _finalize_line(state_dir: Path, dispatch_id: str) -> None:
    """Another finalizer ended this attempt in between two sweeps (intent and all)."""
    _open_intent(state_dir, dispatch_id, row=False)  # the attempt's row already exists
    recovered = lifecycle.recover(state_dir=state_dir, project_id=PROJECT,
                                  dispatch_id=dispatch_id, attempt=ATTEMPT, actor="door")
    assert recovered.status == lifecycle.STATUS_RECOVERED, recovered


def _open_intent(state_dir: Path, dispatch_id: str, *, row: bool = True) -> Path:
    """An end intent frozen but never published, with a row for the projection."""
    if row:
        _insert_row(state_dir, dispatch_id=dispatch_id)
    ledger = _ledger(state_dir)
    prepared = prepare_end_line(
        {"timestamp": START, "event_type": "dispatch_finished", "project_id": PROJECT,
         "dispatch_id": dispatch_id, "attempt": ATTEMPT, "end_state": "completed",
         "head_sha": "c" * 40, "model": "deepseek-flash"},
        ledger_path=ledger)
    path = end_intent_path(state_dir, PROJECT, dispatch_id, ATTEMPT)
    document = build_end_intent(
        project_id=PROJECT, dispatch_id=dispatch_id, attempt=ATTEMPT,
        end_state=str(prepared["end_state"]), end_line=prepared, ledger_path=ledger,
        ledger_offset=ledger.stat().st_size if ledger.exists() else 0)
    assert write_end_intent(path, document) is True
    return path


def _cursor(state_dir: Path) -> dict | None:
    return _heartbeat(state_dir).get("cursor")


def _write_heartbeat(state_dir: Path, cursor: object) -> None:
    """The heartbeat the previous run left behind, with ``cursor`` as its continuation."""
    path = state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")


class _PinnedClock:
    """A monotonic clock that walks a fixed list, then repeats its last value."""

    def __init__(self, values):
        self._values = list(values)
        self._last = self._values[-1] if self._values else 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


# ---------------------------------------------------------------------------
# 1. OI-2522: the start line carries the deadline
# ---------------------------------------------------------------------------

def test_a_spec_under_another_staging_id_puts_its_deadline_on_the_start_line(
        state_dir, data_dir, lifecycle_on):
    dispatch_id = "d-k6b-oi2522"
    spec_file = _staged_spec(data_dir, dispatch_id=dispatch_id,
                             staging_id="staging-not-the-dispatch", deadline=DEADLINE)

    _begin(state_dir, spec_file, dispatch_id=dispatch_id)

    start = _starts(state_dir, dispatch_id)[0]
    assert start["deadline_seconds"] == DEADLINE
    assert lifecycle.deadline_for(state_dir, start, {"bundle_path": None}) == (
        DEADLINE, "start_line")

    _insert_row(state_dir, dispatch_id=dispatch_id)
    result = lifecycle.sweep(state_dir=state_dir, project_id=PROJECT,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 1, result
    assert len(_finished(state_dir, dispatch_id)) == 1


def test_the_start_line_deadline_is_read_before_the_fallback(
        state_dir, data_dir, lifecycle_on):
    """4.5 hours is past 14400 s plus the grace, not past 300 s plus the grace."""
    dispatch_id = "d-k6b-oi2522-fallback"
    spec_file = _staged_spec(data_dir, dispatch_id=dispatch_id,
                             staging_id="staging-elsewhere", deadline=DEADLINE)
    _begin(state_dir, spec_file, dispatch_id=dispatch_id)
    _insert_row(state_dir, dispatch_id=dispatch_id)

    just_past = lifecycle.sweep(
        state_dir=state_dir, project_id=PROJECT,
        now=_now_after(START_DT, DEADLINE + GRACE + 60))
    assert just_past.abandoned == 1, just_past


# ---------------------------------------------------------------------------
# 2. a spec without a valid integer: no field, old lookup order intact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deadline", [True, "300", 300.0, 299, 14401])
def test_a_spec_without_a_valid_deadline_puts_no_field_on_the_start_line(
        state_dir, data_dir, lifecycle_on, deadline):
    dispatch_id = "d-k6b-invalid"
    spec_file = _staged_spec(data_dir, dispatch_id=dispatch_id,
                             staging_id="staging-invalid", deadline=deadline)

    _begin(state_dir, spec_file, dispatch_id=dispatch_id)

    start = _starts(state_dir, dispatch_id)[0]
    assert "deadline_seconds" not in start, start
    # Staged under another staging id: the old candidates cannot see it either.
    assert lifecycle.deadline_for(state_dir, start, {"bundle_path": None}) == (
        lifecycle.FALLBACK_DEADLINE_SECONDS, "fallback:FALLBACK_DEADLINE_SECONDS")


def test_without_a_start_line_field_the_old_lookup_order_still_holds(
        state_dir, lifecycle_on):
    path, spec_sha = _spec(state_dir, deadline=DEADLINE, subdir="completed")
    _start_line(state_dir, spec_sha=spec_sha)

    line = _starts(state_dir)[0]
    assert "deadline_seconds" not in line
    assert lifecycle.deadline_for(state_dir, line, {"bundle_path": None}) == (
        DEADLINE, f"spec:{path}")


# ---------------------------------------------------------------------------
# 3. the budget stops between items and the next sweep continues
# ---------------------------------------------------------------------------

def test_a_spent_budget_stops_between_items_and_the_next_sweep_finishes(
        state_dir, lifecycle_on, monkeypatch):
    dispatch_ids = ["d-k6b-b1", "d-k6b-b2", "d-k6b-b3"]
    for dispatch_id in dispatch_ids:
        _seed_sweepable(state_dir, dispatch_id=dispatch_id)
    ticks = _PinnedClock([0.0, 0.0, 2.0])  # budget, item 0, item 1 -> spent
    monkeypatch.setattr(lifecycle, "_monotonic", ticks)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert first.budget_exhausted is True
    assert first.budget_left == 2
    assert first.abandoned == 1
    ended = [dispatch_id for dispatch_id in dispatch_ids if _finished(state_dir, dispatch_id)]
    assert len(ended) == 1, ended
    assert len(_finished(state_dir, ended[0])) == 1
    heartbeat = _heartbeat(state_dir)
    assert heartbeat["budget"]["exhausted"] is True
    assert heartbeat["budget"]["items_left"] == 2
    assert heartbeat["budget"]["seconds"] == 1.0

    second = lifecycle.sweep(state_dir=state_dir, now=now)

    assert second.abandoned == 2, second
    assert [len(_finished(state_dir, dispatch_id)) for dispatch_id in dispatch_ids] == [1, 1, 1]
    assert _heartbeat(state_dir)["budget"]["exhausted"] is False
    assert _heartbeat(state_dir)["budget"]["items_left"] == 0


# ---------------------------------------------------------------------------
# 3b. K-6b round 2: the continuation cursor of a bounded sweep
# ---------------------------------------------------------------------------

def test_a_bounded_sweep_leaves_the_live_prefix_and_the_next_one_takes_the_dead(
        state_dir, lifecycle_on, monkeypatch):
    """Codex's reproduction: live attempts first, a dead one past its deadline after."""
    live_ids, dead_id = ["d-k6b-c1", "d-k6b-c2"], "d-k6b-c3"
    for dispatch_id in live_ids:
        _attempt(state_dir, dispatch_id, live=True)
    _attempt(state_dir, dead_id, live=False)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.live, first.abandoned, first.budget_exhausted) == (2, 0, True), first
    assert first.budget_left == 1
    assert _finished(state_dir, dead_id) == []
    assert _cursor(state_dir) == {"kind": "attempt", "project_id": PROJECT,
                                  "dispatch_id": dead_id, "attempt": ATTEMPT, "offset": 2}

    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert second.abandoned == 1, second
    assert len(_finished(state_dir, dead_id)) == 1
    assert second.budget_exhausted is False
    assert _cursor(state_dir) is None


def test_the_cursor_at_the_last_item_wraps_to_the_front(state_dir, lifecycle_on, monkeypatch):
    dispatch_ids = ["d-k6b-w1", "d-k6b-w2", "d-k6b-w3"]
    for dispatch_id in dispatch_ids:
        _attempt(state_dir, dispatch_id, live=True)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)
    assert (first.live, first.budget_exhausted) == (2, True), first
    assert _cursor(state_dir)["dispatch_id"] == dispatch_ids[-1]

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 0.0, 0.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert second.live == 3, second  # the cursor's item, then the front, then the rest
    assert second.budget_exhausted is False
    assert _cursor(state_dir) is None


def test_a_cursor_key_finalized_in_between_resumes_at_the_next_item(
        state_dir, lifecycle_on, monkeypatch):
    _attempt(state_dir, "d-k6b-f-live", live=True)
    _attempt(state_dir, "d-k6b-f-one", live=False)
    _attempt(state_dir, "d-k6b-f-two", live=False)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.live, first.abandoned, first.budget_exhausted) == (1, 0, True), first
    assert _cursor(state_dir)["dispatch_id"] == "d-k6b-f-one"

    _finalize_line(state_dir, "d-k6b-f-one")  # someone else ended it in between

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert second.abandoned == 1, second  # one item of budget, spent on f-two
    assert len(_finished(state_dir, "d-k6b-f-two")) == 1
    assert len(_finished(state_dir, "d-k6b-f-one")) == 1  # only the one written above


def test_an_unbounded_sweep_ignores_the_cursor_and_clears_it(
        state_dir, lifecycle_on, monkeypatch):
    dispatch_ids = ["d-k6b-u1", "d-k6b-u2", "d-k6b-u3"]
    for dispatch_id in dispatch_ids:
        _attempt(state_dir, dispatch_id, live=True)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    bounded = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)
    assert bounded.budget_exhausted is True and _cursor(state_dir) is not None, bounded

    unbounded = lifecycle.sweep(state_dir=state_dir, now=now)

    assert unbounded.live == 3, unbounded
    assert unbounded.budget_exhausted is False
    assert _cursor(state_dir) is None


@pytest.mark.parametrize("heartbeat", [
    "{not json at all",
    json.dumps({"cursor": "the front, please"}),
    json.dumps({"cursor": {"project_id": PROJECT, "dispatch_id": "d-k6b-x",
                           "attempt": "1", "offset": 0}}),
    json.dumps({"cursor": {"project_id": PROJECT, "dispatch_id": "d-k6b-x",
                           "attempt": 1}}),
])
def test_an_unreadable_cursor_starts_a_bounded_sweep_at_the_front(
        state_dir, lifecycle_on, monkeypatch, heartbeat):
    dispatch_ids = ["d-k6b-m1", "d-k6b-m2", "d-k6b-m3"]
    for dispatch_id in dispatch_ids:
        _attempt(state_dir, dispatch_id, live=True)
    path = state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(heartbeat, encoding="utf-8")
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    result = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (result.live, result.budget_exhausted) == (1, True), result
    # the front was taken and the item after it recorded: the run started at the front
    assert _cursor(state_dir)["dispatch_id"] == dispatch_ids[1]


def test_a_bounded_run_takes_the_cursors_attempt_and_the_next_one_wraps_to_the_intent(
        state_dir, lifecycle_on, monkeypatch):
    """K-6b r4 (OI-2527, OI-2530): one sequence — intents then attempts — walked from the cursor.

    The intent is before the attempt in the combined sequence, so the cursor's item,
    not the front, decides: a run that can take exactly one item takes the attempt the
    cursor names and leaves the intent frozen. The cursor then wraps past the end to
    that intent, and the next bounded run resumes there and recovers it.
    """
    intent_id, dead_id = "d-k6b-i-intent-1", "d-k6b-i1"
    _open_intent(state_dir, intent_id)
    _attempt(state_dir, dead_id, live=False)
    # offset 0 alone is the front (the intent); the cursor's own item is the attempt
    _write_heartbeat(state_dir, {"kind": "attempt", "project_id": PROJECT,
                                 "dispatch_id": dead_id, "attempt": ATTEMPT, "offset": 0})
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.recovered, first.abandoned, first.budget_exhausted) == (0, 1, True), first
    assert first.budget_left == 1
    ended = _finished(state_dir, dead_id)
    assert len(ended) == 1 and ended[0]["end_state"] == "abandoned", ended
    assert _finished(state_dir, intent_id) == []  # before the cursor's item: not taken
    assert _cursor(state_dir) == {"kind": "intent", "project_id": PROJECT,
                                  "dispatch_id": intent_id, "attempt": ATTEMPT, "offset": 0}

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (second.recovered, second.budget_exhausted) == (1, False), second
    recovered = _finished(state_dir, intent_id)
    assert len(recovered) == 1 and recovered[0]["end_state"] == "completed", recovered
    assert _cursor(state_dir) is None


# ---------------------------------------------------------------------------
# 3c. K-6b round 3: a cursor without a project_id starts at the front
# ---------------------------------------------------------------------------

def test_a_cursor_without_a_project_id_starts_the_next_bounded_sweep_at_the_front(
        state_dir, lifecycle_on, monkeypatch):
    """Codex's reproduction: the heartbeat's cursor has no project_id and work is in flight."""
    dispatch_ids = ["d-k6b-p1", "d-k6b-p2", "d-k6b-p3"]
    for dispatch_id in dispatch_ids:
        _attempt(state_dir, dispatch_id, live=True)
    _write_heartbeat(state_dir, {"dispatch_id": "d", "attempt": 1, "offset": 0})
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.live, first.budget_exhausted) == (1, True), first
    assert _cursor(state_dir)["dispatch_id"] == dispatch_ids[1]  # the front was taken

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 0.0, 0.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (second.live, second.budget_exhausted) == (3, False), second  # its own cursor resumes
    assert _cursor(state_dir) is None


@pytest.mark.parametrize("cursor", [
    {"dispatch_id": "d-k6b-x", "attempt": 1, "offset": 0},  # no project_id (codex)
    {"project_id": "", "dispatch_id": "d-k6b-x", "attempt": 1, "offset": 0},
    {"project_id": PROJECT, "attempt": 1, "offset": 0},  # no dispatch_id
    {"project_id": PROJECT, "dispatch_id": "", "attempt": 1, "offset": 0},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": 0, "offset": 0},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": True, "offset": 0},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": "1", "offset": 0},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": 1, "offset": -1},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": 1, "offset": False},
    {"kind": "bogus", "project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": 1, "offset": 0},
])
def test_read_cursor_rejects_a_cursor_that_cannot_be_resumed(state_dir, cursor):
    _write_heartbeat(state_dir, cursor)

    assert lifecycle._read_cursor(state_dir, None) is None  # an unscoped sweep too
    assert lifecycle._read_cursor(state_dir, PROJECT) is None


def test_read_cursor_accepts_a_well_formed_cursor_only_in_its_own_scope(state_dir):
    good = {"kind": "attempt", "project_id": PROJECT, "dispatch_id": "d-k6b-x",
            "attempt": 1, "offset": 0}
    _write_heartbeat(state_dir, good)

    assert lifecycle._read_cursor(state_dir, None) == good
    assert lifecycle._read_cursor(state_dir, PROJECT) == good
    assert lifecycle._read_cursor(state_dir, "another-project") is None


@pytest.mark.parametrize("cursor", [
    {},
    {"project_id": PROJECT},
    {"dispatch_id": "d-k6b-x", "attempt": 1},
    {"project_id": PROJECT, "dispatch_id": "d-k6b-x", "attempt": 1},
    "not a cursor at all",
])
def test_resume_at_starts_at_the_front_on_a_cursor_missing_a_key(state_dir, lifecycle_on, cursor):
    for dispatch_id in ["d-k6b-r1", "d-k6b-r2"]:
        _attempt(state_dir, dispatch_id, live=True)
    flights, errors = lifecycle.v3_in_flight(state_dir, None)
    assert (len(flights), errors) == (2, ())

    assert lifecycle._resume_at(lifecycle._sequence((), flights), cursor) == 0


def test_a_raising_sweep_never_carries_an_unreadable_cursor_forward(
        state_dir, lifecycle_on, monkeypatch):
    _attempt(state_dir, "d-k6b-e1", live=True)
    _write_heartbeat(state_dir, {"dispatch_id": "d-k6b-e1", "attempt": 1, "offset": 0})
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    def _unreadable(*args, **kwargs):
        raise RuntimeError("store unreadable")

    with monkeypatch.context() as failing:
        failing.setattr(lifecycle, "open_intents", _unreadable)
        with pytest.raises(RuntimeError):
            lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    # the failed run's heartbeat holds the front, not a cursor the next run would choke on
    assert _cursor(state_dir) is None

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 0.0, 0.0]))
    result = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert result.live == 1, result


# ---------------------------------------------------------------------------
# 3d. K-6b round 4 (OI-2527, OI-2530): one bounded sequence over intents + attempts
# ---------------------------------------------------------------------------

def _recover_raises_for(monkeypatch, *dispatch_ids) -> None:
    """The publish lock hangs on these intents: their ``recover`` raises, they stay open."""
    real = lifecycle.recover

    def _recover(**kwargs):
        if kwargs["dispatch_id"] in dispatch_ids:
            raise RuntimeError("publish lock taken")
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "recover", _recover)


def test_a_bounded_run_spent_on_a_failing_intent_resumes_at_the_next_one(
        state_dir, lifecycle_on, monkeypatch):
    """OI-2527: a hanging recover must not cost every bounded run the same intent."""
    first_id, second_id = "d-k6b-r4-i1", "d-k6b-r4-i2"
    _open_intent(state_dir, first_id)
    _open_intent(state_dir, second_id)
    _recover_raises_for(monkeypatch, first_id)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.recovered, first.budget_exhausted) == (0, True), first
    assert any("recover d-k6b-r4-i1" in error for error in first.errors), first.errors
    assert _cursor(state_dir)["kind"] == "intent"
    assert _cursor(state_dir)["dispatch_id"] == second_id
    assert _finished(state_dir, second_id) == []

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert second.recovered == 1, second
    assert len(_finished(state_dir, second_id)) == 1
    assert _finished(state_dir, first_id) == []  # still open, still the next run's item


def test_a_bounded_run_spent_on_a_failing_intent_still_reaches_the_attempt(
        state_dir, lifecycle_on, monkeypatch):
    """OI-2530: within two bounded runs the dead attempt is booked ``abandoned``."""
    intent_id, dead_id = "d-k6b-r4-a-intent", "d-k6b-r4-a-dead"
    _open_intent(state_dir, intent_id)
    _attempt(state_dir, dead_id, live=False)
    _recover_raises_for(monkeypatch, intent_id)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    first = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (first.abandoned, first.budget_exhausted) == (0, True), first
    assert _cursor(state_dir)["kind"] == "attempt"
    assert _cursor(state_dir)["dispatch_id"] == dead_id
    assert _finished(state_dir, dead_id) == []

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    second = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert second.abandoned == 1, second
    ended = _finished(state_dir, dead_id)
    assert len(ended) == 1 and ended[0]["end_state"] == "abandoned", ended


def test_a_k6b_cursor_without_kind_resumes_at_its_attempt(
        state_dir, lifecycle_on, monkeypatch):
    """An upgrade reads the K-6b attempt cursor as an attempt, never as the front."""
    dispatch_ids = ["d-k6b-r4-k1", "d-k6b-r4-k2", "d-k6b-r4-k3"]
    for dispatch_id in dispatch_ids:
        _attempt(state_dir, dispatch_id, live=True)
    # offset 0 alone would be the front; the key is the second attempt (the K-6b format)
    _write_heartbeat(state_dir, {"project_id": PROJECT, "dispatch_id": dispatch_ids[1],
                                 "attempt": ATTEMPT, "offset": 0})
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    monkeypatch.setattr(lifecycle, "_monotonic", _PinnedClock([0.0, 0.0, 2.0]))
    result = lifecycle.sweep(state_dir=state_dir, now=now, budget_seconds=1.0)

    assert (result.live, result.budget_exhausted) == (1, True), result
    assert _cursor(state_dir) == {"kind": "attempt", "project_id": PROJECT,
                                  "dispatch_id": dispatch_ids[2], "attempt": ATTEMPT,
                                  "offset": 2}


def test_an_unbounded_run_after_a_failing_intent_takes_everything_and_clears_the_cursor(
        state_dir, lifecycle_on, monkeypatch):
    bad_id, good_id, dead_id, live_id = (
        "d-k6b-r4-u-bad", "d-k6b-r4-u-good", "d-k6b-r4-u-dead", "d-k6b-r4-u-live")
    _open_intent(state_dir, bad_id)
    _open_intent(state_dir, good_id)
    _attempt(state_dir, dead_id, live=False)
    _attempt(state_dir, live_id, live=True)
    _recover_raises_for(monkeypatch, bad_id)
    _write_heartbeat(state_dir, {"kind": "attempt", "project_id": PROJECT,
                                 "dispatch_id": live_id, "attempt": ATTEMPT, "offset": 3})
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    result = lifecycle.sweep(state_dir=state_dir, now=now)

    assert (result.recovered, result.abandoned, result.live) == (1, 1, 1), result
    assert result.budget_exhausted is False
    assert _cursor(state_dir) is None
    assert len(_finished(state_dir, good_id)) == 1
    assert len(_finished(state_dir, dead_id)) == 1
    assert _finished(state_dir, bad_id) == []
    assert any("recover d-k6b-r4-u-bad" in error for error in result.errors), result.errors


# ---------------------------------------------------------------------------
# 4. the door: one bounded sweep before the hand-over, never a refusal
# ---------------------------------------------------------------------------

def _clean_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=True,
        target_health={"ephemeral": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T1": True},
        model_pins={
            "T0": ModelPin(model="opus", semantics="floor"),
            "T1": ModelPin(model="sonnet", semantics="floor"),
            "T2": ModelPin(model="sonnet", semantics="floor"),
            "T3": ModelPin(model="sonnet", semantics="floor"),
        },
    )


def _make_coordination_db(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    return db_path


def _make_bundle(tmp_path: Path, *, dispatch_id: str) -> "tuple[Path, Path]":
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / f"staging-{dispatch_id}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": PROJECT,
        "dispatch_id": dispatch_id,
        "staging_id": f"staging-{dispatch_id}",
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "base_ref": "origin/main",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _door_env(monkeypatch, data_dir: Path) -> Path:
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    return state_dir


def test_the_door_sweeps_once_with_two_seconds_before_the_hand_over(tmp_path, monkeypatch):
    dispatch_id = "20261103-k6b-door"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    _door_env(monkeypatch, data_dir)

    events: list = []
    seen: dict = {}

    def _sweep(**kwargs):
        events.append("sweep")
        seen.update(kwargs)
        return lifecycle.SweepResult()

    real_hand_over = dispatch_cli._owner_hand_over

    def _hand_over(*args, **kwargs):
        events.append("hand_over")
        return real_hand_over(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "sweep", _sweep)
    monkeypatch.setattr(dispatch_cli, "_owner_hand_over", _hand_over)

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
            patch("dispatch_cli._execute_claude_headless", return_value=0):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    assert seen["budget_seconds"] == 2.0, seen
    assert seen["project_id"] == PROJECT, seen
    assert events[:2] == ["sweep", "hand_over"], events
    assert events.count("sweep") == 1, events


def test_a_raising_door_sweep_never_refuses_or_changes_the_fire(
        tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k6b-door-raise"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    _door_env(monkeypatch, data_dir)

    def _boom(**kwargs):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(lifecycle, "sweep", _boom)

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
            patch("dispatch_cli._execute_claude_headless", return_value=0):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    err = capsys.readouterr().err
    assert "lifecycle sweep failed" in err and "sweep exploded" in err, err


def test_a_dry_run_never_sweeps(tmp_path, monkeypatch):
    dispatch_id = "20261103-k6b-door-dry"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    _door_env(monkeypatch, data_dir)
    calls: list = []

    def _sweep(**kwargs):
        calls.append(kwargs)
        return lifecycle.SweepResult()

    monkeypatch.setattr(lifecycle, "sweep", _sweep)

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()):
        assert dispatch_cli.run_dispatch(spec_file, dry_run=True) == 0

    assert calls == []


# ---------------------------------------------------------------------------
# 5. build_t0_state: one sweep per build, its result in the state
# ---------------------------------------------------------------------------

def _build_dirs(tmp_path: Path) -> "tuple[Path, Path]":
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    return state_dir, dispatch_dir


def test_build_t0_state_sweeps_once_and_reports_the_result(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _build_dirs(tmp_path)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    seen: list = []

    def _sweep(**kwargs):
        seen.append(kwargs)
        return lifecycle.SweepResult(abandoned=2, errors=("one intent unreadable",),
                                     budget_exhausted=True, budget_left=1)

    monkeypatch.setattr(lifecycle, "sweep", _sweep)

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert len(seen) == 1, seen
    assert seen[0]["budget_seconds"] == 2.0, seen
    assert state["lifecycle_sweep"] == {
        "abandoned": 2, "errors": 1, "budget_exhausted": True}, state["lifecycle_sweep"]


def test_a_raising_build_sweep_leaves_the_build_exit_code_unchanged(
        tmp_path, monkeypatch):
    state_dir, dispatch_dir = _build_dirs(tmp_path)
    out = tmp_path / "t0_state.json"
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setattr(sys, "argv", ["build_t0_state.py", "--output", str(out)])
    monkeypatch.setattr(bts, "_STATE_DIR", state_dir)
    monkeypatch.setattr(bts, "_DISPATCH_DIR", dispatch_dir)
    writes: list = []
    monkeypatch.setattr(bts, "_write_atomic", lambda path, data: writes.append(path))
    monkeypatch.setattr(bts, "_write_all_state_outputs", lambda *_a, **_k: False)
    monkeypatch.setattr(bts, "_emit_build_signal", lambda *_a, **_k: None)
    monkeypatch.setattr(bts, "_emit_health_beacon", lambda *_a, **_k: None)

    monkeypatch.setattr(lifecycle, "sweep", lambda **kw: lifecycle.SweepResult())
    baseline = bts.main()
    baseline_writes = len(writes)

    def _boom(**kwargs):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(lifecycle, "sweep", _boom)
    raising = bts.main()

    assert raising == baseline, (raising, baseline)
    assert raising != bts._EXIT_BUILD_FAILED
    assert len(writes) > baseline_writes  # the build still produced its state


# ---------------------------------------------------------------------------
# 6. two overlapping sweeps on one dead attempt give one end line
# ---------------------------------------------------------------------------

def test_two_overlapping_sweeps_on_one_dead_attempt_give_one_end(state_dir, lifecycle_on):
    _seed_sweepable(state_dir)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)
    barrier = threading.Barrier(2)
    results: list = []
    failures: list = []

    def _run():
        try:
            barrier.wait(timeout=30)
            results.append(lifecycle.sweep(state_dir=state_dir, project_id=PROJECT, now=now))
        except Exception as exc:  # pytest records the assertion below from the main thread
            failures.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == [], failures
    assert len(results) == 2, results
    assert sum(result.abandoned for result in results) == 1, results
    assert len(_finished(state_dir)) == 1
