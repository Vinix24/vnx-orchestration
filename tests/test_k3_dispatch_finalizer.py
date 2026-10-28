"""ADR-045 (audit-spoor-kern K-3): the end finalizer and the append rule.

Dispatch-ID: dlv-032c1b2d300c

What this file pins, deterministically:

  - two processes finalizing the same ``(project_id, dispatch_id, attempt)``
    produce exactly one intent and exactly one ledger line; the loser writes
    nothing and says so;
  - a crash after the ledger line and before the projection, and a crash after
    the projection and before ``published_at``, both recover to one line, the
    target state and ``published_at`` — no second line;
  - every row of the plan table "Eindtoestand naar projectie" reaches its target
    from every listed begin state with one event per step, and a row in another
    terminal state (or without a path) stays put and reports a
    ``projection_conflict``;
  - two projects with the same ``dispatch_id`` touch only the own row and the
    own event;
  - attempt 2 gets its own intent and its own end line; attempt 1's intent is
    byte-unchanged;
  - the append rule: a ``dispatch_finished`` is accepted only when a winner's
    intent exists for the key AND its canonical form is byte-equal to the
    intent's line. Another ``end_state`` with the same ``outcome_id`` is
    refused, and so is any end line without an intent;
  - the finalizer validates what it finalizes (round 2): an identifier that is
    not one safe path component, an end payload that is not this attempt's
    ``dispatch_finished``, a receipt field the intent never decided, and the
    ``abandoned`` mark on an already-``timed_out`` row.

The coordination DB is the real schema built by the project's own helpers in
``tmp_path`` (``init_schema`` + the project_id column migration + the 0017
composite-UNIQUE rebuild), and the ledger is the real append layer. The only
things patched are the two crash injections (``_project`` and
``_publish_intent``); no path outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import append_receipt  # noqa: E402
import dispatch_lifecycle as dl  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
    load_end_intent,
)
from append_receipt_internals.outcome_identity import compute_outcome_id  # noqa: E402
from coordination_db import get_connection, init_schema  # noqa: E402
from project_id_migration import (  # noqa: E402
    RUNTIME_COORDINATION_TABLES,
    apply_project_id_migration,
)

_APPLY_0017 = VNX_ROOT / "scripts" / "lib" / "migrations" / "apply_0017.py"
_spec = importlib.util.spec_from_file_location("apply_0017_k3", _APPLY_0017)
_apply_0017 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_apply_0017)

PROJECT = "vnx-dev"
OTHER_PROJECT = "another-project"
DISPATCH = "d-k3"
START = "dispatch_started"
FINISH = "dispatch_finished"


# ---------------------------------------------------------------------------
# Fixtures: the real schema, the real ledger, the real append layer
# ---------------------------------------------------------------------------

def _init_db(state_dir: Path) -> None:
    """The real runtime-coordination DB with ADR-007's composite key on dispatches.

    ``init_schema`` (the project's init helper) creates the base tables; the
    project_id column comes from the real migration, and the real 0017 rebuild
    replaces the v1 single-column ``UNIQUE(dispatch_id)`` with
    ``UNIQUE(dispatch_id, project_id)`` — without it the two-projects case would
    be impossible to construct (and impossible in production, ADR-007).
    """
    init_schema(state_dir)
    with get_connection(state_dir) as conn:
        apply_project_id_migration(
            conn, RUNTIME_COORDINATION_TABLES, default_project_id=PROJECT)
        _apply_0017._rebuild_dispatches(conn)
        conn.commit()


@pytest.fixture
def lifecycle_on(monkeypatch):
    """The setting the append layer requires for any lifecycle line."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture
def state_dir(tmp_path: Path, lifecycle_on) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _init_db(state_dir)
    return state_dir


def _ledger(state_dir: Path) -> Path:
    return dl.ledger_path_for(state_dir)


def _append(state_dir: Path, receipt: dict):
    return append_receipt.append_receipt_payload(
        dict(receipt), receipts_file=str(_ledger(state_dir)), skip_enrichment=True,
    )


def _lines(state_dir: Path) -> list[dict]:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _start_line(project_id: str = PROJECT, dispatch_id: str = DISPATCH,
                attempt: int = 1, mode: str = "v3") -> dict:
    return {
        "timestamp": "2026-10-28T09:00:00Z",
        "event_type": START,
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "role": "backend-developer",
        "base_sha": "a" * 40,
        "spec_sha256": "b" * 64,
        "requested_by": "T0",
        "lifecycle_mode": mode,
        "door_pid": 4242,
        "door_started_at": "2026-10-28T08:59:59Z",
    }


def _end_line(end_state: str, project_id: str = PROJECT, dispatch_id: str = DISPATCH,
              attempt: int = 1, head_sha: str = "c" * 40) -> dict:
    return {
        "timestamp": "2026-10-28T10:00:00Z",
        "event_type": FINISH,
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "end_state": end_state,
        "head_sha": head_sha,
    }


def _insert_dispatch(state_dir: Path, state: str, project_id: str = PROJECT,
                     dispatch_id: str = DISPATCH, attempt_count: int = 1) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count) "
            "VALUES (?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count),
        )
        conn.commit()


def _row(state_dir: Path, project_id: str = PROJECT, dispatch_id: str = DISPATCH) -> dict:
    with get_connection(state_dir) as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()
    return dict(row)


def _events(state_dir: Path, dispatch_id: str = DISPATCH) -> list[dict]:
    with get_connection(state_dir) as conn:
        rows = conn.execute(
            "SELECT * FROM coordination_events WHERE entity_type = 'dispatch' "
            "AND entity_id = ? ORDER BY occurred_at, rowid", (dispatch_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _end_lines(state_dir: Path) -> list[dict]:
    return [line for line in _lines(state_dir) if line.get("event_type") == FINISH]


def _finalizable(state_dir: Path, state: str = "running") -> None:
    """A v3 dispatch with a start line and a DB row — the state finalize expects."""
    _insert_dispatch(state_dir, state)
    assert _append(state_dir, _start_line()).status == "appended"


# ---------------------------------------------------------------------------
# 1. one intent, one ledger line, across processes (requirement 2)
# ---------------------------------------------------------------------------

def _child_finalize(state_dir: str, end_state: str, barrier, queue) -> None:
    barrier.wait(timeout=20)
    try:
        result = dl.finalize(
            PROJECT, DISPATCH, 1, _end_line(end_state), state_dir=state_dir)
        queue.put((result.status, result.ledger_written, result.end_state))
    except Exception as exc:  # vnx-silent-except: the failure is reported to the parent
        queue.put(("error", False, f"{type(exc).__name__}: {exc}"))


def test_two_processes_finalize_one_intent_one_line(state_dir: Path):
    _finalizable(state_dir)
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    children = [
        ctx.Process(target=_child_finalize, args=(str(state_dir), end_state, barrier, queue))
        for end_state in ("completed", "failed")
    ]
    for child in children:
        child.start()
    outcomes = [queue.get(timeout=30) for _ in children]
    for child in children:
        child.join(timeout=30)

    assert sorted(o[0] for o in outcomes) == [dl.LOST_INTENT, dl.PUBLISHED], outcomes
    assert [o[1] for o in outcomes if o[0] == dl.PUBLISHED] == [True]

    intents = list((state_dir / "dispatch_end" / PROJECT).glob(f"{DISPATCH}.1.json"))
    assert len(intents) == 1, intents
    assert len(_end_lines(state_dir)) == 1
    winning = _end_lines(state_dir)[0]
    assert _row(state_dir)["state"] == dl.PROJECTION_TARGET[winning["end_state"]]


# ---------------------------------------------------------------------------
# 2. recovery after a crash (steps 3-4 and 4-5)
# ---------------------------------------------------------------------------

def test_crash_between_ledger_and_projection_recovers(state_dir: Path, monkeypatch):
    _finalizable(state_dir)
    real_project = dl._project
    calls = {"n": 0}

    def boom_once(intent, state_dir_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash after the ledger line")
        return real_project(intent, state_dir_arg)

    monkeypatch.setattr(dl, "_project", boom_once)
    with pytest.raises(RuntimeError):
        dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)

    intent_file = end_intent_path(state_dir, PROJECT, DISPATCH, 1)
    assert intent_file.exists()
    assert load_end_intent(intent_file).get("published_at") is None
    assert len(_end_lines(state_dir)) == 1

    monkeypatch.setattr(dl, "_project", real_project)
    recovered = dl.recover(PROJECT, DISPATCH, 1, state_dir=state_dir)

    assert recovered.status == dl.PUBLISHED
    assert recovered.ledger_written is False
    assert recovered.projection["status"] == dl.PROJECTION_APPLIED
    assert len(_end_lines(state_dir)) == 1
    assert _row(state_dir)["state"] == "completed"
    assert load_end_intent(intent_file)["published_at"]


def test_crash_between_projection_and_publication_recovers(state_dir: Path, monkeypatch):
    _finalizable(state_dir)
    real_publish = dl._publish_intent

    def boom_once(intent_file, intent):
        raise RuntimeError("injected crash after the projection")

    monkeypatch.setattr(dl, "_publish_intent", boom_once)
    with pytest.raises(RuntimeError):
        dl.finalize(PROJECT, DISPATCH, 1, _end_line("timed_out"), state_dir=state_dir)

    intent_file = end_intent_path(state_dir, PROJECT, DISPATCH, 1)
    assert load_end_intent(intent_file).get("published_at") is None
    assert _row(state_dir)["state"] == "timed_out"
    assert len(_end_lines(state_dir)) == 1
    events_after_crash = [e for e in _events(state_dir) if e["event_type"] != "dispatch_noop"]
    assert len(events_after_crash) == 1

    monkeypatch.setattr(dl, "_publish_intent", real_publish)
    recovered = dl.recover(PROJECT, DISPATCH, 1, state_dir=state_dir)

    assert recovered.status == dl.PUBLISHED
    assert recovered.ledger_written is False
    assert recovered.projection["status"] == dl.PROJECTION_ALREADY_AT_TARGET
    assert len(_end_lines(state_dir)) == 1
    assert load_end_intent(intent_file)["published_at"]
    assert len([e for e in _events(state_dir) if e["event_type"] != "dispatch_noop"]) == 1


# ---------------------------------------------------------------------------
# 3. the projection table: every row from every listed begin state
# ---------------------------------------------------------------------------

PROJECTION_CASES = [
    (end_state, from_state, path)
    for end_state, by_state in dl.PROJECTION_PATHS.items()
    for from_state, path in by_state.items()
]


@pytest.mark.parametrize("end_state,from_state,path", PROJECTION_CASES)
def test_each_projection_row_reaches_its_target_with_one_event_per_step(
    state_dir: Path, end_state, from_state, path,
):
    _finalizable(state_dir, state=from_state)
    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line(end_state), state_dir=state_dir)

    target = dl.PROJECTION_TARGET[end_state]
    assert result.projection["status"] == dl.PROJECTION_APPLIED
    assert result.projection["steps"] == list(path)
    assert _row(state_dir)["state"] == target

    steps = [e for e in _events(state_dir) if e["event_type"] != "dispatch_noop"]
    transitions = [(e["from_state"], e["to_state"]) for e in steps]
    expected = list(zip((from_state, *path[:-1]), path))
    assert transitions == expected
    for event in steps:
        assert event["actor"] == dl.PROJECTION_ACTOR
        assert event["project_id"] == PROJECT


def test_abandoned_projects_to_timed_out_with_the_mark(state_dir: Path):
    _finalizable(state_dir, state="running")
    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line("abandoned"), state_dir=state_dir)

    assert result.projection["status"] == dl.PROJECTION_APPLIED
    row = _row(state_dir)
    assert row["state"] == "timed_out"
    assert json.loads(row["metadata_json"])["end_state"] == "abandoned"


@pytest.mark.parametrize("from_state,reason", [
    ("dead_letter", "terminal_state"),
    ("expired", "terminal_state"),
    ("queued", "no_path"),
    ("failed_delivery", "no_path"),
])
def test_a_row_without_a_path_stays_and_reports_a_conflict(
    state_dir: Path, from_state, reason,
):
    _finalizable(state_dir, state=from_state)
    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)

    assert result.status == dl.PUBLISHED
    assert result.projection["status"] == dl.PROJECTION_CONFLICT
    assert result.projection["reason"] == reason
    assert _row(state_dir)["state"] == from_state
    assert [e for e in _events(state_dir) if e["event_type"] != "dispatch_noop"] == []
    assert len(_end_lines(state_dir)) == 1


# ---------------------------------------------------------------------------
# 4. two projects, one dispatch_id (requirement 1)
# ---------------------------------------------------------------------------

def test_two_projects_with_one_dispatch_id_never_touch_each_other(state_dir: Path):
    _insert_dispatch(state_dir, "running", project_id=PROJECT)
    _insert_dispatch(state_dir, "running", project_id=OTHER_PROJECT)
    _append(state_dir, _start_line())
    _append(state_dir, _start_line(project_id=OTHER_PROJECT))

    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)

    assert result.status == dl.PUBLISHED
    assert _row(state_dir, PROJECT)["state"] == "completed"
    assert _row(state_dir, OTHER_PROJECT)["state"] == "running"

    other_events = [e for e in _events(state_dir) if e["project_id"] == OTHER_PROJECT]
    assert other_events == []
    own_events = [e for e in _events(state_dir) if e["project_id"] == PROJECT]
    assert [(e["from_state"], e["to_state"]) for e in own_events] == [("running", "completed")]

    # The other project's own finalize then works on its own row only.
    second = dl.finalize(
        OTHER_PROJECT, DISPATCH, 1, _end_line("completed", project_id=OTHER_PROJECT),
        state_dir=state_dir)
    assert second.status == dl.PUBLISHED
    assert _row(state_dir, OTHER_PROJECT)["state"] == "completed"
    assert len(_end_lines(state_dir)) == 2


# ---------------------------------------------------------------------------
# 5. attempt 2 (requirement 2)
# ---------------------------------------------------------------------------

def test_attempt_two_gets_its_own_intent_and_line(state_dir: Path):
    _insert_dispatch(state_dir, "running")
    _append(state_dir, _start_line(attempt=1))
    _append(state_dir, _start_line(attempt=2, mode="v3", dispatch_id=DISPATCH))

    first = dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)
    assert first.status == dl.PUBLISHED
    attempt_one_intent = end_intent_path(state_dir, PROJECT, DISPATCH, 1)
    attempt_one_bytes = attempt_one_intent.read_bytes()

    second = dl.finalize(PROJECT, DISPATCH, 2, _end_line("failed", attempt=2), state_dir=state_dir)

    assert second.status == dl.PUBLISHED
    assert second.intent_path == end_intent_path(state_dir, PROJECT, DISPATCH, 2)
    assert second.intent_path.exists()
    assert attempt_one_intent.read_bytes() == attempt_one_bytes
    assert [line["attempt"] for line in _end_lines(state_dir)] == [1, 2]
    assert [line["end_state"] for line in _end_lines(state_dir)] == ["completed", "failed"]


# ---------------------------------------------------------------------------
# 6. the append rule: intent + byte-equality (requirement 4)
# ---------------------------------------------------------------------------

def _write_intent(state_dir: Path, end_line: dict) -> Path:
    intent_file = end_intent_path(
        state_dir, end_line["project_id"], end_line["dispatch_id"], end_line["attempt"])
    document = build_end_intent(
        project_id=end_line["project_id"], dispatch_id=end_line["dispatch_id"],
        attempt=end_line["attempt"], end_state=end_line["end_state"],
        end_line=end_line, ledger_path=_ledger(state_dir),
        ledger_offset=_ledger(state_dir).stat().st_size if _ledger(state_dir).exists() else 0,
    )
    assert dl.create_intent(intent_file, document) is True
    return intent_file


def test_an_end_line_without_an_intent_is_refused(state_dir: Path):
    assert _append(state_dir, _start_line()).status == "appended"
    with pytest.raises(AppendReceiptError) as excinfo:
        _append(state_dir, _end_line("completed"))
    assert excinfo.value.code == "missing_end_intent"
    assert _end_lines(state_dir) == []


def test_another_end_state_with_the_same_outcome_id_is_refused(state_dir: Path):
    """``timed_out`` and ``abandoned`` project to the same status, so the same outcome_id."""
    expected = _end_line("timed_out")
    other = _end_line("abandoned")
    expected["status"] = other["status"] = "timeout"
    assert compute_outcome_id(expected) == compute_outcome_id(other), (
        "premise: the two lines must share an outcome_id, or this test proves nothing")

    assert _append(state_dir, _start_line()).status == "appended"
    _write_intent(state_dir, expected)

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(state_dir, other)
    assert excinfo.value.code == "end_line_mismatch"
    assert _end_lines(state_dir) == []

    # The line the intent decided is accepted, byte-for-byte.
    assert _append(state_dir, expected).status == "appended"
    assert len(_end_lines(state_dir)) == 1


def test_an_end_line_that_matches_the_intent_is_accepted_and_a_replay_is_discarded(state_dir: Path):
    assert _append(state_dir, _start_line()).status == "appended"
    line = _end_line("completed")
    _write_intent(state_dir, line)
    assert _append(state_dir, line).status == "appended"

    # A byte-identical replay is the same line: the append layer's own dedup
    # discards it (status "duplicate") — never a second line, never a refusal.
    assert _append(state_dir, line).status == "duplicate"
    assert len(_end_lines(state_dir)) == 1


# ---------------------------------------------------------------------------
# 7. the mode gate comes from the start line, never from the setting
# ---------------------------------------------------------------------------

def test_a_legacy_dispatch_is_not_finalized_here(state_dir: Path):
    _insert_dispatch(state_dir, "running")
    _append(state_dir, _start_line(mode="legacy"))

    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)

    assert result.status == dl.NOT_V3
    assert result.ledger_written is False
    assert not end_intent_path(state_dir, PROJECT, DISPATCH, 1).exists()
    assert _end_lines(state_dir) == []
    assert _row(state_dir)["state"] == "running"


def test_a_dispatch_without_a_start_line_is_refused(state_dir: Path):
    _insert_dispatch(state_dir, "running")
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)
    assert excinfo.value.code == "missing_start_line"
    assert _end_lines(state_dir) == []


# ---------------------------------------------------------------------------
# 8. the finalizer validates what it finalizes (round 2)
#
# The four reproductions from the codex gate on cb1519b1: an identifier that is
# not one path component, an end payload that is not this attempt's
# dispatch_finished, a receipt field the intent never decided, and the
# abandoned mark skipped by the already-at-target return.
# ---------------------------------------------------------------------------

UNSAFE_IDENTIFIERS = ["..", "../x", "a/b", "a\\b", "nul\x00byte", "", "   "]


@pytest.mark.parametrize("dispatch_id", UNSAFE_IDENTIFIERS)
def test_an_unsafe_dispatch_id_is_refused_and_creates_nothing(state_dir: Path,
                                                              dispatch_id):
    _finalizable(state_dir)
    ledger_before = _ledger(state_dir).read_bytes()
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, dispatch_id, 1,
                    _end_line("completed", dispatch_id=dispatch_id), state_dir=state_dir)
    assert excinfo.value.code == "invalid_identifier"
    assert _row(state_dir)["state"] == "running"
    assert _ledger(state_dir).read_bytes() == ledger_before
    assert _end_lines(state_dir) == []
    assert not (state_dir / "dispatch_end").exists()


def test_an_absolute_dispatch_id_is_refused_and_creates_nothing_outside(state_dir: Path,
                                                                       tmp_path: Path):
    _finalizable(state_dir)
    outside = str(tmp_path / "escaped-dispatch")
    before = sorted(tmp_path.iterdir())
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, outside, 1, _end_line("completed", dispatch_id=outside),
                    state_dir=state_dir)
    assert excinfo.value.code == "invalid_identifier"
    # Nothing was created beside ``state/``: no intent, no lock.
    assert sorted(tmp_path.iterdir()) == before
    assert not Path(f"{outside}.1.json").exists()
    assert not Path(f"{outside}.1.lock").exists()
    assert _row(state_dir)["state"] == "running"
    assert _end_lines(state_dir) == []


@pytest.mark.parametrize("project_id", UNSAFE_IDENTIFIERS)
def test_an_unsafe_project_id_is_refused_and_creates_nothing(state_dir: Path, project_id):
    _finalizable(state_dir)
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(project_id, DISPATCH, 1,
                    _end_line("completed", project_id=project_id), state_dir=state_dir)
    assert excinfo.value.code == "invalid_identifier"
    assert _row(state_dir)["state"] == "running"
    assert not (state_dir / "dispatch_end").exists()


def test_recover_refuses_an_unsafe_identifier_too(state_dir: Path, tmp_path: Path):
    outside = str(tmp_path / "recovered-elsewhere")
    before = sorted(tmp_path.iterdir())
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.recover(PROJECT, outside, 1, state_dir=state_dir)
    assert excinfo.value.code == "invalid_identifier"
    assert sorted(tmp_path.iterdir()) == before
    assert not Path(f"{outside}.1.json").exists()
    assert not Path(f"{outside}.1.lock").exists()


def test_a_resolved_intent_path_outside_dispatch_end_is_refused(state_dir: Path, tmp_path: Path):
    """The second guard: a symlinked project directory resolves out of the root."""
    root = state_dir / "dispatch_end"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / PROJECT).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, DISPATCH, 1, _end_line("completed"), state_dir=state_dir)

    assert excinfo.value.code == "invalid_identifier"
    assert list(elsewhere.iterdir()) == []


def test_a_start_line_as_the_end_payload_is_refused_and_changes_nothing(state_dir: Path):
    _finalizable(state_dir)
    ledger_before = _ledger(state_dir).read_bytes()
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, DISPATCH, 1, dict(_start_line(), end_state="completed"),
                    state_dir=state_dir)
    assert excinfo.value.code == "invalid_end_line"
    assert _row(state_dir)["state"] == "running"
    assert _ledger(state_dir).read_bytes() == ledger_before
    assert _end_lines(state_dir) == []
    assert not end_intent_path(state_dir, PROJECT, DISPATCH, 1).exists()
    assert not (state_dir / "dispatch_end").exists()


@pytest.mark.parametrize("override", [
    {"project_id": OTHER_PROJECT},
    {"dispatch_id": "another-dispatch"},
    {"attempt": 2},
    {"end_state": "gave_up"},
    {"event_type": "task_complete"},
    {"attempt": True},
])
def test_an_end_payload_that_does_not_match_the_key_is_refused(state_dir: Path, override):
    _finalizable(state_dir)
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, DISPATCH, 1, dict(_end_line("completed"), **override),
                    state_dir=state_dir)
    assert excinfo.value.code == "invalid_end_line"
    assert _row(state_dir)["state"] == "running"
    assert _end_lines(state_dir) == []
    assert not (state_dir / "dispatch_end").exists()


def test_an_end_payload_that_is_not_a_mapping_is_refused(state_dir: Path):
    _finalizable(state_dir)
    with pytest.raises(dl.DispatchLifecycleError) as excinfo:
        dl.finalize(PROJECT, DISPATCH, 1, ["not", "a", "mapping"], state_dir=state_dir)
    assert excinfo.value.code == "invalid_end_line"
    assert _end_lines(state_dir) == []


def test_a_receipt_field_the_intent_never_decided_is_a_mismatch(state_dir: Path):
    """The codex reproduction: an added ``no_push`` against an intent without one."""
    assert _append(state_dir, _start_line()).status == "appended"
    line = _end_line("completed")
    _write_intent(state_dir, line)

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(state_dir, dict(line, no_push="nothing to push"))
    assert excinfo.value.code == "end_line_mismatch"
    assert _end_lines(state_dir) == []

    # The line the intent decided is still accepted.
    assert _append(state_dir, line).status == "appended"
    assert len(_end_lines(state_dir)) == 1


# One plausible value per derived stamp, so a stamp added to the append layer
# without a name in DERIVED_STAMP_FIELDS fails this test.
STAMP_VALUES = {
    "schema_version": 2,
    "writer_sha": "f" * 40,
    "status": "success",
    "operator_id": "operator",
    "orchestrator_id": "T0",
    "agent_id": "deepseek-flash",
    "observability_tier": "tier-low",
    "parent_dispatch": "dlv-parent",
    "task_class": "01_code_generation",
    "tier_from": "tier-low",
    "tier_to": "tier-low",
    "open_items_created": 2,
    "ingested_at": "2020-01-01T00:00:00Z",
    "outcome_id": "0" * 64,
}


def test_the_derived_stamps_the_append_layer_adds_are_not_a_mismatch(state_dir: Path):
    from append_receipt_internals.lifecycle import DERIVED_STAMP_FIELDS

    assert set(STAMP_VALUES) == DERIVED_STAMP_FIELDS
    assert _append(state_dir, _start_line()).status == "appended"
    line = _end_line("completed")
    _write_intent(state_dir, line)

    assert _append(state_dir, dict(line, **STAMP_VALUES)).status == "appended"
    end_lines = _end_lines(state_dir)
    assert len(end_lines) == 1
    assert end_lines[0]["end_state"] == "completed"


def test_abandoned_from_an_already_timed_out_row_sets_the_mark(state_dir: Path):
    _finalizable(state_dir, state="timed_out")

    result = dl.finalize(PROJECT, DISPATCH, 1, _end_line("abandoned"), state_dir=state_dir)

    assert result.projection["status"] == dl.PROJECTION_ALREADY_AT_TARGET
    row = _row(state_dir)
    assert row["state"] == "timed_out"
    assert json.loads(row["metadata_json"])["end_state"] == "abandoned"
    assert len(_end_lines(state_dir)) == 1

    # A second finalize is still a no-op: the intent is already published.
    again = dl.finalize(PROJECT, DISPATCH, 1, _end_line("abandoned"), state_dir=state_dir)
    assert again.status == dl.LOST_INTENT
    assert _row(state_dir) == row
    assert len(_end_lines(state_dir)) == 1


def test_the_abandoned_mark_refuses_a_row_that_is_not_there(state_dir: Path):
    """No row means the mark has nowhere to land: that is a refusal, not a no-op.

    Unreachable through ``finalize`` (the projection names the row first), so
    the branch is pinned at the helper that owns it.
    """
    with get_connection(state_dir) as conn:
        with pytest.raises(dl.DispatchLifecycleError) as excinfo:
            dl._mark_abandoned(
                conn, {"dispatch_id": "no-such-dispatch", "project_id": PROJECT})
    assert excinfo.value.code == "missing_dispatch_row"
