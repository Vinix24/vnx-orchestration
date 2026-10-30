"""ADR-045 (audit-spoor-kern K-3c): ``finalize`` and ``recover`` on K-3a/K-3b.

Dispatch-ID: dlv-0d21b069c42c

What this file pins, one block per requirement of the dispatch:

1. one intent and one end line, also when two processes race for the same
   attempt (the exclusive ``os.link`` freeze);
2. ``recover`` completes a crash between the ledger and the projection and one
   between the projection and ``published_at``;
3. every row of ADR-045 §4's "Eindtoestand naar projectie", from every start
   state it lists, and a conflict for every state it does not;
4. two projects that share a ``dispatch_id`` never touch each other's row;
5. a late attempt 1 never ends attempt 2 (OI-2443: the attempt is the key);
6. an unsafe ``dispatch_id`` is refused before anything is written;
7. ``abandoned`` is a ``timed_out`` row plus ``metadata_json.end_state``;
8. an end line with another outcome than the intent's is refused;
9. the frozen event type and the frozen warning classification survive the
   pre-write hook (OI-2470, OI-2452);
10. a corrupt intent and a NaN/Infinity float are named refusals.

Every store lives in ``tmp_path``; nothing here touches the real ledger.
"""

from __future__ import annotations

import fcntl
import importlib.util as _ilu
import json
import multiprocessing
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import project_id_migration  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
from append_receipt_internals.common import AppendReceiptError, AppendResult  # noqa: E402
from append_receipt_internals.idempotency import (  # noqa: E402
    _cache_file_for,
    _write_receipt_under_lock,
)
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
    end_line_matches_intent,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)
from append_receipt_internals.outcome_identity import compute_outcome_id  # noqa: E402
from append_receipt_internals.payload import append_receipt_payload  # noqa: E402
from coordination_db import get_connection  # noqa: E402

PROJECT = "vnx-dev"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k3c"
ATTEMPT = 1

END_LINE = {
    "timestamp": "2026-10-30T10:00:00Z",
    "event_type": "dispatch_finished",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": ATTEMPT,
    "end_state": "completed",
    "head_sha": "c" * 40,
    "model": "deepseek-flash",
}

WARNING = {"code": "review_probe", "severity": "warn", "message": "probe"}

# apply_0017 is the project's own migration that replaces the pre-ADR-007
# single-column UNIQUE(dispatch_id) with UNIQUE(dispatch_id, project_id).
_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k3c", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = tmp_path / "state"
    sd.mkdir()
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


def _line(**overrides) -> dict:
    return dict(END_LINE, **overrides)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _append(state_dir: Path, line: dict):
    return append_receipt_payload(dict(line), receipts_file=str(_ledger(state_dir)),
                                  skip_enrichment=True)


def _insert_row(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                state: str = "running", attempt_count: int = ATTEMPT,
                metadata: str = "{}") -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count, metadata),
        )
        conn.commit()


def _row(state_dir: Path, dispatch_id: str = DISPATCH, project_id: str = PROJECT) -> dict:
    with get_connection(state_dir) as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()
    return dict(row) if row is not None else {}


def _freeze(state_dir: Path, line: dict, *, project_id: str = PROJECT,
            dispatch_id: str = DISPATCH, attempt: int = ATTEMPT) -> Path:
    """Freeze an intent the way ``finalize`` does, without appending or projecting."""
    ledger = _ledger(state_dir)
    prepared = prepare_end_line(line, ledger_path=ledger)
    intent_path = end_intent_path(state_dir, project_id, dispatch_id, attempt)
    offset = ledger.stat().st_size if ledger.exists() else 0
    document = build_end_intent(
        project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
        end_state=str(prepared["end_state"]), end_line=prepared,
        ledger_path=ledger, ledger_offset=offset,
    )
    assert write_end_intent(intent_path, document) is True
    return intent_path


def _finalize(state_dir: Path, **overrides) -> lifecycle.LifecycleResult:
    kwargs = dict(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                  attempt=ATTEMPT, end_state="completed", end_line=_line())
    kwargs.update(overrides)
    return lifecycle.finalize(**kwargs)


def _recover(state_dir: Path, **overrides) -> lifecycle.LifecycleResult:
    kwargs = dict(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=ATTEMPT)
    kwargs.update(overrides)
    return lifecycle.recover(**kwargs)


# ---------------------------------------------------------------------------
# 1. one intent, one end line — also across processes
# ---------------------------------------------------------------------------

def test_finalize_writes_one_intent_one_line_and_projects_the_row(state_dir, lifecycle_on):
    _insert_row(state_dir)

    result = _finalize(state_dir)

    assert result.status == lifecycle.STATUS_FINALIZED
    assert result.line_status == "appended"
    assert result.end_state == "completed"
    assert result.projection["action"] == lifecycle.PROJECTION_APPLIED
    assert result.published_at

    assert len(_lines(state_dir)) == 1
    assert _lines(state_dir)[0]["event_type"] == "dispatch_finished"
    assert _lines(state_dir)[0]["status"] == "success"
    assert _row(state_dir)["state"] == "completed"

    intent = load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))
    assert intent["published_at"] == result.published_at
    assert end_line_matches_intent(_lines(state_dir)[0], intent)


def _finalize_in_a_child(state_dir: str, results) -> None:
    try:
        result = lifecycle.finalize(
            state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=ATTEMPT,
            end_state="completed", end_line=dict(END_LINE),
        )
        results.put(("ok", result.status))
    except Exception as exc:  # surface the child's failure to the parent
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_two_processes_on_one_dispatch_freeze_one_intent_and_write_one_line(state_dir, lifecycle_on):
    _insert_row(state_dir)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    children = [context.Process(target=_finalize_in_a_child, args=(str(state_dir), results))
                for _ in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=60)

    outcomes = [results.get(timeout=5) for _ in children]
    assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
    statuses = sorted(status for _, status in outcomes)
    assert statuses[0] == lifecycle.STATUS_FINALIZED
    assert statuses[1] in (lifecycle.STATUS_INTENT_LOST, lifecycle.STATUS_ALREADY_PUBLISHED)

    intents = list((state_dir / "dispatch_end" / PROJECT).glob("*.json"))
    assert len(intents) == 1
    assert len(_lines(state_dir)) == 1
    assert _row(state_dir)["state"] == "completed"


def test_the_intent_is_frozen_before_the_append_runs(state_dir, lifecycle_on, monkeypatch):
    """The K-3b contract leaves ``write_end_intent`` outside the append lock, so
    ``finalize`` must freeze first and append second (the order is the guarantee)."""
    _insert_row(state_dir)
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    seen = {}

    def checked_append(line, **kwargs):
        seen["intent"] = load_end_intent(intent_path)
        seen["ledger_lines"] = _lines(state_dir)
        return real_append(line, **kwargs)

    real_append = append_receipt_payload
    monkeypatch.setattr("append_receipt_internals.payload.append_receipt_payload", checked_append)

    _finalize(state_dir)

    assert seen["intent"] is not None
    assert seen["intent"].get("published_at") is None
    assert seen["ledger_lines"] == []


# ---------------------------------------------------------------------------
# 2. recover: the two crash windows
# ---------------------------------------------------------------------------

def test_recover_completes_a_crash_between_the_ledger_and_the_projection(
    state_dir, lifecycle_on, monkeypatch,
):
    _insert_row(state_dir)

    def refuse_to_project(*args, **kwargs):
        raise RuntimeError("crash after the append, before the projection")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_project", refuse_to_project)
        with pytest.raises(RuntimeError):
            _finalize(state_dir)

    assert len(_lines(state_dir)) == 1
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)).get(
        "published_at") is None
    assert _row(state_dir)["state"] == "running"

    result = _recover(state_dir)

    assert result.status == lifecycle.STATUS_RECOVERED
    assert result.line_status == lifecycle.LINE_ALREADY_IN_LEDGER
    assert result.projection["action"] == lifecycle.PROJECTION_APPLIED
    assert len(_lines(state_dir)) == 1
    assert _row(state_dir)["state"] == "completed"
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))["published_at"]


def test_recover_completes_a_crash_between_the_projection_and_publication(
    state_dir, lifecycle_on, monkeypatch,
):
    _insert_row(state_dir)

    def refuse_to_publish(*args, **kwargs):
        raise RuntimeError("crash after the projection, before published_at")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_publish_intent", refuse_to_publish)
        with pytest.raises(RuntimeError):
            _finalize(state_dir)

    assert _row(state_dir)["state"] == "completed"
    assert len(_lines(state_dir)) == 1
    assert "published_at" not in load_end_intent(
        end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))

    result = _recover(state_dir)

    assert result.status == lifecycle.STATUS_RECOVERED
    assert result.projection["action"] == lifecycle.PROJECTION_NOOP
    assert len(_lines(state_dir)) == 1
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))["published_at"]


def test_recover_without_an_intent_reports_no_intent(state_dir, lifecycle_on):
    _insert_row(state_dir)
    assert _recover(state_dir).status == lifecycle.STATUS_NO_INTENT
    assert _lines(state_dir) == []


def test_recover_leaves_a_published_intent_alone(state_dir, lifecycle_on):
    _insert_row(state_dir)
    first = _finalize(state_dir)

    again = _recover(state_dir)

    assert again.status == lifecycle.STATUS_ALREADY_PUBLISHED
    assert again.published_at == first.published_at
    assert len(_lines(state_dir)) == 1


# ---------------------------------------------------------------------------
# 3. the projection table, literally (ADR-045 §4)
# ---------------------------------------------------------------------------

PROJECTION_CASES = [
    # end_state, start state, expected state
    ("completed", "running", "completed"),
    ("completed", "accepted", "completed"),
    ("completed", "delivering", "completed"),
    ("failed", "delivering", "failed_delivery"),
    ("failed", "running", "failed_delivery"),
    ("failed", "claimed", "failed_delivery"),
    ("failed", "accepted", "failed_delivery"),
    ("timed_out", "delivering", "timed_out"),
    ("timed_out", "accepted", "timed_out"),
    ("timed_out", "running", "timed_out"),
    ("timed_out", "claimed", "timed_out"),
    ("abandoned", "delivering", "timed_out"),
    ("abandoned", "accepted", "timed_out"),
    ("abandoned", "running", "timed_out"),
    ("abandoned", "claimed", "timed_out"),
]


@pytest.mark.parametrize("end_state,start_state,expected", PROJECTION_CASES)
def test_every_projection_row_from_every_listed_start_state(
    state_dir, lifecycle_on, end_state, start_state, expected,
):
    _insert_row(state_dir, state=start_state)

    result = _finalize(state_dir, end_state=end_state, end_line=_line(end_state=end_state))

    assert result.projection["action"] == lifecycle.PROJECTION_APPLIED
    assert result.projection["from_state"] == start_state
    assert _row(state_dir)["state"] == expected


@pytest.mark.parametrize("end_state", ["completed", "failed", "timed_out", "abandoned"])
def test_a_row_already_in_the_target_state_is_a_no_op(state_dir, lifecycle_on, end_state):
    target = lifecycle.END_STATE_TARGET[end_state]
    _insert_row(state_dir, state=target)

    result = _finalize(state_dir, end_state=end_state, end_line=_line(end_state=end_state))

    assert result.projection["action"] == lifecycle.PROJECTION_NOOP
    assert _row(state_dir)["state"] == target


@pytest.mark.parametrize("end_state,start_state", [
    ("completed", "claimed"),      # no path to completed from claimed
    ("completed", "queued"),
    ("failed", "queued"),
    ("timed_out", "ready"),
    ("abandoned", "proposed"),
    ("completed", "expired"),      # another terminal state
    ("failed", "dead_letter"),
    ("timed_out", "withdrawn"),
])
def test_a_row_the_table_does_not_list_is_a_conflict_and_stays(
    state_dir, lifecycle_on, end_state, start_state,
):
    _insert_row(state_dir, state=start_state)

    result = _finalize(state_dir, end_state=end_state, end_line=_line(end_state=end_state))

    assert result.projection["action"] == lifecycle.PROJECTION_CONFLICT
    assert _row(state_dir)["state"] == start_state
    # The ledger is the source: the line lands even when the row cannot follow.
    assert len(_lines(state_dir)) == 1
    assert result.published_at


def test_a_missing_row_is_reported_and_the_line_still_lands(state_dir, lifecycle_on):
    result = _finalize(state_dir)
    assert result.projection["action"] == lifecycle.PROJECTION_NO_ROW
    assert len(_lines(state_dir)) == 1


def test_a_missing_store_is_reported_and_the_line_still_lands(tmp_path, lifecycle_on):
    empty = tmp_path / "empty-state"
    empty.mkdir()

    result = _finalize(empty)

    assert result.projection["action"] == lifecycle.PROJECTION_NO_STORE
    assert len(_lines(empty)) == 1


# ---------------------------------------------------------------------------
# 4/5. the key: project and attempt
# ---------------------------------------------------------------------------

def test_two_projects_with_the_same_dispatch_id_only_the_own_row_changes(state_dir, lifecycle_on):
    _insert_row(state_dir, project_id=PROJECT)
    _insert_row(state_dir, project_id=OTHER_PROJECT)

    result = _finalize(state_dir)

    assert result.projection["action"] == lifecycle.PROJECTION_APPLIED
    assert _row(state_dir, project_id=PROJECT)["state"] == "completed"
    assert _row(state_dir, project_id=OTHER_PROJECT)["state"] == "running"


def test_a_late_attempt_1_does_not_end_attempt_2(state_dir, lifecycle_on):
    _insert_row(state_dir, attempt_count=2)

    result = _finalize(state_dir, attempt=1, end_line=_line(attempt=1))

    assert result.projection["action"] == lifecycle.PROJECTION_STALE_ATTEMPT
    assert result.projection["row_attempt_count"] == 2
    assert _row(state_dir)["state"] == "running"
    # The line for attempt 1 is still the ledger's business; the row is attempt 2's.
    assert len(_lines(state_dir)) == 1
    assert _lines(state_dir)[0]["attempt"] == 1


def test_a_row_that_never_claimed_an_attempt_is_never_projected(state_dir, lifecycle_on):
    _insert_row(state_dir, state="queued", attempt_count=0)

    result = _finalize(state_dir)

    assert result.projection["action"] == lifecycle.PROJECTION_STALE_ATTEMPT
    assert result.projection["row_attempt_count"] == 0
    assert _row(state_dir)["state"] == "queued"


def test_attempt_1_and_attempt_2_each_get_their_own_intent_and_line(state_dir, lifecycle_on):
    _insert_row(state_dir, attempt_count=1)
    _finalize(state_dir, attempt=1, end_line=_line(attempt=1))

    with get_connection(state_dir) as conn:
        conn.execute("UPDATE dispatches SET attempt_count = 2, state = 'running'")
        conn.commit()
    result = _finalize(state_dir, attempt=2, end_line=_line(attempt=2, timestamp="2026-10-30T11:00:00Z"))

    assert result.projection["action"] == lifecycle.PROJECTION_APPLIED
    assert len(_lines(state_dir)) == 2
    assert [line["attempt"] for line in _lines(state_dir)] == [1, 2]
    assert len(list((state_dir / "dispatch_end" / PROJECT).glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# 6. the key is validated before any path is written
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dispatch_id", ["../x", "a/b", "a\\b", "nul\x00byte", ".."])
def test_an_unsafe_dispatch_id_is_refused_before_anything_is_written(
    state_dir, lifecycle_on, dispatch_id,
):
    _insert_row(state_dir)
    before = _row(state_dir)

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, dispatch_id=dispatch_id, end_line=_line(dispatch_id=dispatch_id))

    assert excinfo.value.code == "invalid_identifier"
    assert not (state_dir / "dispatch_end").exists()
    assert _lines(state_dir) == []
    assert _row(state_dir) == before


def test_an_absolute_dispatch_id_is_refused_before_anything_is_written(state_dir, lifecycle_on, tmp_path):
    _insert_row(state_dir)
    outside = str(tmp_path / "escaped-dispatch")

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, dispatch_id=outside, end_line=_line(dispatch_id=outside))

    assert excinfo.value.code == "invalid_identifier"
    assert not (state_dir / "dispatch_end").exists()
    assert _lines(state_dir) == []
    assert not Path(dirname_output := f"{outside}.{ATTEMPT}.json").exists(), dirname_output


def test_an_unsafe_project_id_is_refused_before_anything_is_written(state_dir, lifecycle_on):
    _insert_row(state_dir)

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, project_id="../other", end_line=_line(project_id="../other"))

    assert excinfo.value.code == "invalid_identifier"
    assert not (state_dir / "dispatch_end").exists()
    assert _lines(state_dir) == []


def test_a_ledger_outside_the_state_dir_is_refused(state_dir, lifecycle_on, tmp_path):
    _insert_row(state_dir)
    elsewhere = tmp_path / "elsewhere" / "t0_receipts.ndjson"

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, ledger_path=elsewhere)

    assert excinfo.value.code == "ledger_outside_state"
    assert not elsewhere.exists()
    assert not (state_dir / "dispatch_end").exists()


# ---------------------------------------------------------------------------
# 7. abandoned: the ledger fact and the projection marker
# ---------------------------------------------------------------------------

def test_abandoned_projects_to_timed_out_with_the_metadata_marker(state_dir, lifecycle_on):
    _insert_row(state_dir, state="running", metadata='{"lifecycle_mode": "v3"}')

    result = _finalize(state_dir, end_state="abandoned", end_line=_line(end_state="abandoned"))

    row = _row(state_dir)
    assert row["state"] == "timed_out"
    assert json.loads(row["metadata_json"]) == {"lifecycle_mode": "v3", "end_state": "abandoned"}
    assert result.projection["end_state"] == "abandoned"
    assert _lines(state_dir)[0]["end_state"] == "abandoned"
    assert _lines(state_dir)[0]["status"] == "timeout"


def test_abandoned_re_stamps_an_unpublished_intent_even_when_the_line_exists(state_dir, lifecycle_on):
    _insert_row(state_dir, state="running")
    with get_connection(state_dir) as conn:
        conn.execute("UPDATE dispatches SET attempt_count = 1")
        conn.commit()
    intent_path = _freeze(state_dir, _line(end_state="abandoned"))
    # The line landed and the row moved, but the intent never published and the
    # marker was never written (a crash after step 4's transitions).
    _append(state_dir, json.loads(load_end_intent(intent_path)["line"]))
    with get_connection(state_dir) as conn:
        conn.execute("UPDATE dispatches SET state = 'timed_out' WHERE dispatch_id = ?", (DISPATCH,))
        conn.commit()

    result = _recover(state_dir)

    assert result.line_status == lifecycle.LINE_ALREADY_IN_LEDGER
    assert result.projection["metadata"] == "marked"
    assert json.loads(_row(state_dir)["metadata_json"])["end_state"] == "abandoned"


def test_an_unreadable_metadata_json_is_refused_and_the_transaction_rolls_back(state_dir, lifecycle_on):
    _insert_row(state_dir, state="running", metadata="{not json")

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, end_state="abandoned", end_line=_line(end_state="abandoned"))

    assert excinfo.value.code == "projection_metadata_unreadable"
    assert _row(state_dir)["state"] == "running"
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)).get(
        "published_at") is None


# ---------------------------------------------------------------------------
# 8. one outcome per attempt: another outcome is refused
# ---------------------------------------------------------------------------

def test_a_dispatch_finished_whose_outcome_differs_from_the_intent_is_refused(state_dir, lifecycle_on):
    _insert_row(state_dir)
    _freeze(state_dir, _line(end_state="completed"))
    completed = dict(END_LINE, status="success")
    failed = dict(END_LINE, status="failure", end_state="failed")
    assert compute_outcome_id(completed) != compute_outcome_id(failed)

    with pytest.raises(AppendReceiptError) as excinfo:
        _append(state_dir, failed)

    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(state_dir) == []


def test_the_loser_of_the_freeze_writes_nothing(state_dir, lifecycle_on):
    _insert_row(state_dir)
    _finalize(state_dir)

    again = _finalize(state_dir, end_line=_line(timestamp="2026-10-30T12:00:00Z"))

    assert again.status == lifecycle.STATUS_ALREADY_PUBLISHED
    assert len(_lines(state_dir)) == 1
    assert _lines(state_dir)[0]["timestamp"] == END_LINE["timestamp"]


# ---------------------------------------------------------------------------
# 9. the append's own guards: frozen event type, frozen classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entering_type,rewritten_type", [
    ("dispatch_finished", "task_complete"),
    ("task_complete", "dispatch_finished"),
])
def test_a_pre_write_hook_cannot_change_whether_this_line_is_an_end_line(
    state_dir, lifecycle_on, entering_type, rewritten_type,
):
    _insert_row(state_dir)
    prepared_line = _line()
    intent_path = _freeze(state_dir, prepared_line)
    ledger = _ledger(state_dir)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    receipt = json.loads(load_end_intent(intent_path)["line"])
    receipt["event_type"] = entering_type

    def rewrite_the_type(receipt_in_hook: dict) -> None:
        receipt_in_hook["event_type"] = rewritten_type

    with pytest.raises(AppendReceiptError) as excinfo:
        _write_receipt_under_lock(
            receipt, ledger, _cache_file_for(ledger), "k3c-event-type-freeze", 300,
            pre_write_hook=rewrite_the_type,
        )

    assert excinfo.value.code == "end_line_mismatch"
    assert _lines(state_dir) == []


def _counter_path(state_dir: Path) -> Path:
    return state_dir / "warning_recurrence_counts.json"


def _set_counter(state_dir: Path, code: str, count: int) -> None:
    _counter_path(state_dir).write_text(json.dumps({code: count}), encoding="utf-8")


def test_a_threshold_crossing_after_the_freeze_is_written_against_the_frozen_class(
    state_dir, lifecycle_on,
):
    """OI-2452: the counter crosses its threshold between freeze and append."""
    _insert_row(state_dir)
    line = _line(warnings=[dict(WARNING)])
    intent_path = _freeze(state_dir, line)
    frozen = json.loads(load_end_intent(intent_path)["line"])
    assert frozen["warnings"][0]["requires_tracking"] is False
    _set_counter(state_dir, WARNING["code"], 9)

    result = _recover(state_dir)

    assert result.line_status == "appended"
    written = _lines(state_dir)[0]
    assert written["warnings"][0]["requires_tracking"] is False
    assert written["warnings"][0]["destination"] == "counted"
    assert written["warnings"][0]["reason"] is None
    assert end_line_matches_intent(written, load_end_intent(intent_path)) is True
    # The deferred commit followed the frozen classification: counted, not an OI.
    assert json.loads(_counter_path(state_dir).read_text(encoding="utf-8"))[WARNING["code"]] == 10


def test_the_frozen_classification_survives_a_retry_after_the_threshold_crossed(
    state_dir, lifecycle_on, monkeypatch,
):
    """A refusal in the first write must not leave the key permanently unappendable."""
    _insert_row(state_dir)
    line = _line(warnings=[dict(WARNING)])
    _freeze(state_dir, line)
    _set_counter(state_dir, WARNING["code"], 9)

    def fail_the_first_write(receipt, **kwargs):
        raise AppendReceiptError("test_refusal", 11, "the first write fails")

    with monkeypatch.context() as patch:
        patch.setattr("append_receipt_internals.payload.commit_receipt_v2_fields",
                      fail_the_first_write)
        with pytest.raises(AppendReceiptError):
            _append(state_dir, line)

    assert _lines(state_dir) == []
    assert _append(state_dir, line).status == "appended"
    assert _lines(state_dir)[0]["warnings"][0]["requires_tracking"] is False


# ---------------------------------------------------------------------------
# 10. named refusals: a corrupt intent, an unrepresentable float
# ---------------------------------------------------------------------------

def test_an_intent_that_is_not_utf8_is_a_named_refusal(state_dir, lifecycle_on):
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    intent_path.parent.mkdir(parents=True)
    intent_path.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(AppendReceiptError) as excinfo:
        _recover(state_dir)

    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(state_dir) == []


def test_an_intent_that_cannot_be_read_is_a_named_refusal(state_dir, lifecycle_on):
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    intent_path.mkdir(parents=True)  # a directory where the intent file belongs

    with pytest.raises(AppendReceiptError) as excinfo:
        _recover(state_dir)

    assert excinfo.value.code == "invalid_end_intent"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_float_json_cannot_represent_is_a_named_refusal(state_dir, lifecycle_on, value):
    _insert_row(state_dir)

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir, end_line=_line(duration_seconds=value))

    assert excinfo.value.code == "invalid_end_line"
    assert not (state_dir / "dispatch_end").exists()
    assert _lines(state_dir) == []


def test_a_malformed_intent_is_a_named_refusal_when_the_freeze_loses(state_dir, lifecycle_on):
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(AppendReceiptError) as excinfo:
        _finalize(state_dir)

    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(state_dir) == []


def test_a_malformed_intent_is_a_named_refusal_in_recover_too(state_dir, lifecycle_on):
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text('{"dispatch_id": "x"}', encoding="utf-8")  # reads, not an intent

    with pytest.raises(AppendReceiptError) as excinfo:
        _recover(state_dir)

    assert excinfo.value.code == "invalid_end_intent"
    assert _lines(state_dir) == []


# ---------------------------------------------------------------------------
# 11. a torn ledger tail: the end line must start on its own line (K-3c r2)
# ---------------------------------------------------------------------------

TORN_FRAGMENT = b'{"timestamp":"2026-10-30T09:00:00Z","event_type":"dispatch_fin'


def _parseable_end_lines(state_dir: Path) -> list:
    """Every parseable ``dispatch_finished`` line for the test key, whole ledger."""
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    found = []
    for raw in ledger.read_bytes().split(b"\n"):
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (isinstance(line, dict)
                and line.get("event_type") == "dispatch_finished"
                and (line.get("project_id"), line.get("dispatch_id"), line.get("attempt"))
                == (PROJECT, DISPATCH, ATTEMPT)):
            found.append(line)
    return found


def test_a_torn_tail_keeps_the_fragment_and_recover_writes_one_end_line(
    state_dir, lifecycle_on,
):
    """The retry must not be glued onto the fragment: that swallowed the end line
    and recover then published a row with no valid end record (codex blocker)."""
    _insert_row(state_dir)
    ledger = _ledger(state_dir)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(TORN_FRAGMENT)
    _freeze(state_dir, _line())

    result = _recover(state_dir)

    assert result.status == lifecycle.STATUS_RECOVERED
    assert result.line_status == "appended"
    # The append-only ledger keeps the torn fragment byte-for-byte.
    assert ledger.read_bytes().startswith(TORN_FRAGMENT)
    assert ledger.read_bytes()[len(TORN_FRAGMENT):len(TORN_FRAGMENT) + 1] == b"\n"
    assert len(_parseable_end_lines(state_dir)) == 1
    assert _row(state_dir)["state"] == "completed"
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))["published_at"]


def test_a_missing_end_line_after_the_append_is_a_named_refusal_and_recover_retries(
    state_dir, lifecycle_on, monkeypatch,
):
    """An append that reports ``appended`` but writes nothing must never project:
    the ledger read-back, not the return status, is the evidence (codex blocker)."""
    _insert_row(state_dir)
    _freeze(state_dir, _line())

    def append_writes_nothing(receipt, **kwargs):
        return AppendResult(status="appended", receipts_file=_ledger(state_dir),
                            idempotency_key="k3c-readback")

    with monkeypatch.context() as patch:
        patch.setattr("append_receipt_internals.payload.append_receipt_payload",
                      append_writes_nothing)
        with pytest.raises(AppendReceiptError) as excinfo:
            _recover(state_dir)

    assert excinfo.value.code == "end_line_not_verified"
    assert _row(state_dir)["state"] == "running"
    assert "published_at" not in load_end_intent(
        end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))

    again = _recover(state_dir)

    assert again.status == lifecycle.STATUS_RECOVERED
    assert len(_parseable_end_lines(state_dir)) == 1
    assert _row(state_dir)["state"] == "completed"
    assert load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))["published_at"]


def test_append_lock_contention_is_bounded_by_the_caller_timeout(state_dir, lifecycle_on):
    """``lock_timeout`` bounds the whole operation, not only the publication lock."""
    _insert_row(state_dir)
    _freeze(state_dir, _line())
    lock_path = state_dir / "append_receipt.lock"
    held = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)

    def release_after_a_second() -> None:
        time.sleep(0.5)
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    releaser = threading.Thread(target=release_after_a_second)
    releaser.start()
    try:
        started = time.monotonic()
        with pytest.raises(AppendReceiptError) as excinfo:
            _recover(state_dir, lock_timeout=0.02)
        elapsed = time.monotonic() - started
    finally:
        releaser.join(timeout=5)
        held.close()

    assert excinfo.value.code == "lock_timeout"
    assert elapsed < 1.0
    assert _parseable_end_lines(state_dir) == []
    assert "published_at" not in load_end_intent(
        end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))
