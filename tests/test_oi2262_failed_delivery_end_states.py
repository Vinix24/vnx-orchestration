"""OI-2262: a rejected or superseded attempt ends in a terminal state; a base closes on its own round.

Two holes kept a track out of `done` and a delivered base unbookable on the vnx-dev
store (measured 27-10): `receipt_query.py decide <id> reject` wrote only a decision-log
line, so a `failed_delivery`/`timed_out` dispatch row stayed non-terminal forever, and
`vnx deliverable close` required `ready`, so a base whose work landed under its own
`<id>-rK` round could not be booked at all.

Real CLI, real store (the live-store fixture: dispatches without the 0022 CHECK, the
deliverables view of the terminal manifest version), real state machine.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coordination_db
import planning_cli
import receipt_query
import schema_manifest
import track_reconciler
import tracks as tracks_lib
from migrations import apply_0037

from conftest import pin_vnx_data_dir
from fixtures.live_store_fixture import build_live_store

PID, TRACK = "test-proj", "feat-source"
SQL_0037 = _ROOT / "schemas" / "migrations" / "0037_deliverable_superseded.sql"
REASON = "spawn failure; the work landed under a later round"


# ---------------------------------------------------------------------------
# store, CLI and read helpers
# ---------------------------------------------------------------------------

def _conn(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    return conn


def _sql(store: Path, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = _conn(store)
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


def _row(store: Path, dispatch_id: str) -> Dict[str, Any]:
    return (_sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                 (dispatch_id, PID)) or [{}])[0]


def _events(store: Path, dispatch_id: str, *event_types: str) -> List[Dict[str, Any]]:
    """The coordination events of one dispatch, optionally only those of the given types."""
    rows = _sql(store, "SELECT * FROM coordination_events WHERE entity_id = ? ORDER BY id", (dispatch_id,))
    return [r for r in rows if not event_types or r["event_type"] in event_types]


def _decisions(store: Path, dispatch_id: str) -> List[Dict[str, Any]]:
    path = store / "t0_decision_log.jsonl"
    if not path.is_file():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in lines if r.get("dispatch_id") == dispatch_id]


def _view(store: Path, deliverable_ref: str) -> str:
    rows = _sql(store, "SELECT derived_status FROM deliverables WHERE deliverable_ref = ?", (deliverable_ref,))
    assert len(rows) == 1, rows
    return rows[0]["derived_status"]


def _user_version(store: Path) -> int:
    return _sql(store, "PRAGMA user_version")[0]["user_version"]


def _derived(store: Path, merged: frozenset = frozenset({2241}), track_id: str = TRACK) -> str:
    conn = _conn(store)
    try:
        return track_reconciler._compute_derived_status(conn, track_id, PID, merged_pr_numbers=merged)
    finally:
        conn.close()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pin_vnx_data_dir(monkeypatch, tmp_path / "data")
    state_dir = tmp_path / "data" / "state"
    build_live_store(state_dir)
    tracks_lib.create_track(state_dir, TRACK, PID, TRACK, f"ship {TRACK}", phase="queued",
                            pr_ref="#2241", metadata_json=json.dumps({"plan_scope": "light"}))
    return state_dir


def _cli(store: Path, *argv: str, pid: str = PID) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main([*argv, "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _decide(store: Path, dispatch_id: str, decision: str, reason: str = REASON, pid: str = PID) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = receipt_query.main(["decide", dispatch_id, decision, "--reason", reason,
                                 "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _add(store: Path, promote: bool = False) -> str:
    rc, out, err = _cli(store, "deliverable", "add", "--objective", TRACK, "--output-kind", "pr", "--title", "t")
    assert rc == 0, err
    dlv = out.splitlines()[0].split(": ", 1)[1]
    if promote:
        assert _cli(store, "deliverable", "promote", dlv)[0] == 0
    return dlv


def _set_state(store: Path, dispatch_id: str, state: str) -> str:
    _sql(store, "UPDATE dispatches SET state = ? WHERE dispatch_id = ? AND project_id = ?",
         (state, dispatch_id, PID))
    return dispatch_id


def _add_round(store: Path, base: str, number: int = 3, state: str = "completed",
               project: str = PID, pr_ref: Optional[str] = "#2262") -> str:
    round_id = f"{base}-r{number}"
    _sql(store, "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, '{}')", (round_id, project, state, TRACK, pr_ref))
    return round_id


def _close(store: Path, dispatch_id: str, evidence: str = "2262", *extra: str, pid: str = PID) -> tuple:
    return _cli(store, "deliverable", "close", dispatch_id, "--evidence", evidence, *extra, pid=pid)


# ---------------------------------------------------------------------------
# 1. A T0 reject ends the attempt
# ---------------------------------------------------------------------------

def test_a_reject_moves_a_failed_delivery_row_to_dead_letter_with_one_event(store):
    dlv = _set_state(store, _add(store), "failed_delivery")

    rc, out, err = _decide(store, dlv, "reject")

    assert rc == 0, err
    assert _row(store, dlv)["state"] == "dead_letter"
    [event] = _events(store, dlv, "dispatch_dead_letter")
    assert (event["event_type"], event["from_state"], event["to_state"], event["actor"], event["project_id"]) == \
        ("dispatch_dead_letter", "failed_delivery", "dead_letter", "t0", PID)
    assert event["reason"] == REASON
    [decision] = _decisions(store, dlv)
    assert (decision["decision"], decision["project_id"], decision["reasoning"]) == ("reject", PID, REASON)
    metadata = json.loads(event["metadata_json"])
    assert (metadata["decision"], metadata["decided_at"], metadata["project_id"]) == \
        ("reject", decision["timestamp"], PID)
    assert f"{dlv}: failed_delivery -> dead_letter" in out


def test_a_reject_moves_a_timed_out_row_to_dead_letter(store):
    dlv = _set_state(store, _add(store), "timed_out")
    assert _decide(store, dlv, "reject")[0] == 0
    assert (_row(store, dlv)["state"], len(_events(store, dlv, "dispatch_dead_letter"))) == ("dead_letter", 1)


@pytest.mark.parametrize("state", ["completed", "proposed", "ready", "queued", "claimed", "delivering",
                                   "accepted", "running", "expired", "recovered", "withdrawn"])
def test_a_reject_on_any_other_state_records_the_decision_and_leaves_the_row(store, state):
    dlv = _set_state(store, _add(store), state)
    before, events_before = _row(store, dlv), _events(store, dlv)

    rc, out, err = _decide(store, dlv, "reject")

    assert rc == 0, err
    assert _row(store, dlv) == before and _events(store, dlv) == events_before
    assert [d["decision"] for d in _decisions(store, dlv)] == ["reject"]
    assert "untouched (state " in out


def test_a_second_reject_on_a_dead_letter_row_is_a_no_op(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    assert _decide(store, dlv, "reject")[0] == 0
    after_first, events_after_first = _row(store, dlv), _events(store, dlv)

    rc, out, err = _decide(store, dlv, "reject", reason="second look, same judgement")

    assert rc == 0, err
    assert _row(store, dlv) == after_first and _events(store, dlv) == events_after_first
    assert len(_events(store, dlv, "dispatch_dead_letter")) == 1
    assert [d["reasoning"] for d in _decisions(store, dlv)] == [REASON, "second look, same judgement"]
    assert "untouched (state 'dead_letter'" in out


def test_a_reject_never_ends_another_projects_row(store):
    dlv = _add(store)
    _sql(store, "UPDATE dispatches SET project_id = 'other-proj', state = 'failed_delivery' WHERE dispatch_id = ?",
         (dlv,))
    before = _sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ?", (dlv,))

    rc, out, err = _decide(store, dlv, "reject")

    assert rc == 0, err
    assert _sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ?", (dlv,)) == before
    assert "untouched (no dispatch of this project by that id)" in out


def test_a_reject_outside_a_store_still_records_the_decision(tmp_path, monkeypatch):
    pin_vnx_data_dir(monkeypatch, tmp_path / "data")
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    rc, out, err = _decide(state_dir, "dlv-not-in-a-store", "reject")
    assert rc == 0, err
    assert "untouched (no runtime_coordination.db" in out
    assert [d["decision"] for d in _decisions(state_dir, "dlv-not-in-a-store")] == ["reject"]


def test_a_track_with_one_rejected_failed_round_derives_done_after_the_reject(store):
    _set_state(store, _add(store), "completed")
    _set_state(store, _add(store), "completed")
    rejected = _set_state(store, _add(store), "failed_delivery")
    _sql(store, "UPDATE dispatches SET pr_ref = '#2241' WHERE project_id = ? AND track = ?", (PID, TRACK))

    assert _derived(store) == "queued"  # the failed attempt is neither terminal nor in flight

    assert _decide(store, rejected, "reject")[0] == 0

    assert _derived(store) == "done"


def test_a_dead_letter_row_on_a_failed_transition_exits_non_zero_and_keeps_the_decision(store, monkeypatch):
    dlv = _set_state(store, _add(store), "failed_delivery")
    real = coordination_db.DISPATCH_TRANSITIONS["failed_delivery"]
    monkeypatch.setitem(coordination_db.DISPATCH_TRANSITIONS, "failed_delivery", frozenset({"recovered"}))

    rc, out, err = _decide(store, dlv, "reject")

    assert real == frozenset({"recovered", "expired", "dead_letter"})
    assert rc == 1
    assert "not moved to dead_letter" in err and "dead_letter" in err
    assert _row(store, dlv)["state"] == "failed_delivery"
    assert _events(store, dlv, "dispatch_dead_letter") == []
    assert [d["decision"] for d in _decisions(store, dlv)] == ["reject"]


# ---------------------------------------------------------------------------
# 2. A superseded base closes on its own round
# ---------------------------------------------------------------------------

def test_close_of_a_failed_base_with_a_completed_round_is_done_in_view_and_list(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    round_id = _add_round(store, dlv, 3)

    rc, out, err = _close(store, dlv)

    assert rc == 0, err
    row = _row(store, dlv)
    assert (row["state"], row["pr_ref"]) == ("expired", "#2262")
    metadata = json.loads(row["metadata_json"])
    assert (metadata["superseded_by"], metadata["superseded_from"], metadata["superseded_evidence_pr"],
            metadata["attestation"]) == (round_id, "failed_delivery", 2262, "operator")
    assert metadata["deliverable"] is True  # merged into the row's metadata, never replaced
    [event] = _events(store, dlv, "deliverable_superseded")
    assert (event["from_state"], event["to_state"], event["actor"], event["project_id"]) == \
        ("failed_delivery", "expired", "operator", PID)
    assert json.loads(event["metadata_json"])["superseded_by"] == round_id
    assert f"{round_id}" in out

    assert _view(store, f"pr:{dlv}") == "done"
    listed = {r["deliverable_ref"]: r["derived_status"]
              for r in json.loads(_cli(store, "deliverable", "list", "--objective", TRACK, "--json")[1])}
    assert listed[f"pr:{dlv}"] == "done"


def test_close_of_a_timed_out_base_with_a_completed_round_is_accepted(store):
    dlv = _set_state(store, _add(store), "timed_out")
    _add_round(store, dlv, 2)

    rc, out, err = _close(store, dlv, "2259", "--json")

    assert rc == 0, err
    payload = json.loads(out)
    assert (payload["from_state"], payload["to_state"], payload["action"], payload["applied"]) == \
        ("timed_out", "expired", "closed_on_round", True)
    assert (payload["superseded_by"], payload["evidence_pr"], payload["pr_ref"]) == (f"{dlv}-r2", 2259, "#2259")
    assert _view(store, f"pr:{dlv}") == "done"


def test_close_of_a_failed_base_without_a_completed_round_is_refused(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    before = _row(store, dlv)

    rc, out, err = _close(store, dlv)

    assert rc == 2
    assert f"no round {dlv}-rK is 'completed'" in err and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []
    assert _view(store, f"pr:{dlv}") == "failed"


@pytest.mark.parametrize("round_state", ["timed_out", "failed_delivery", "dead_letter", "expired",
                                         "withdrawn", "ready", "running", "", "Completed"])
def test_close_of_a_failed_base_whose_round_did_not_complete_is_refused(store, round_state):
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3, state=round_state)
    before = _row(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []
    assert _view(store, f"pr:{dlv}") == "failed"


def test_close_of_a_failed_base_is_refused_when_the_completed_round_is_another_projects(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3, project="other-proj")
    before = _row(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []


def test_close_of_a_failed_base_is_refused_when_its_metadata_does_not_parse(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3)
    _sql(store, "UPDATE dispatches SET metadata_json = 'not json' WHERE dispatch_id = ?", (dlv,))
    before = _row(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "metadata_json does not parse" in err and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []


def test_close_of_a_ready_deliverable_is_unchanged_even_with_a_completed_round(store):
    dlv = _add(store, promote=True)
    _add_round(store, dlv, 2)

    rc, out, err = _close(store, dlv, "2241")

    assert rc == 0, err
    assert _row(store, dlv)["state"] == "completed"
    [event] = _events(store, dlv, "deliverable_completed")
    assert event["to_state"] == "completed"
    assert json.loads(_row(store, dlv)["metadata_json"]).get("superseded_by") is None


def test_close_of_an_already_closed_deliverable_stays_a_no_op(store):
    dlv = _set_state(store, _add(store), "completed")
    rc, out, err = _close(store, dlv, "2262", "--json")
    assert rc == 0, err
    assert json.loads(out)["action"] == "noop_already_closed"


def test_close_of_an_expired_base_is_refused_by_name(store):
    dlv = _set_state(store, _add(store), "expired")
    rc, _, err = _close(store, dlv)
    assert rc == 2 and "found 'expired'" in err and "No change made." in err


def test_a_superseded_base_is_a_delivery_signal_for_the_track(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3)
    _sql(store, "UPDATE dispatches SET pr_ref = '#2241' WHERE project_id = ? AND track = ?", (PID, TRACK))

    assert _derived(store, merged=frozenset()) == "queued"

    assert _close(store, dlv)[0] == 0

    # the operator attested the round's PR at close: that event is the track's delivery signal
    assert _derived(store, merged=frozenset()) == "done"


# ---------------------------------------------------------------------------
# 3. The view: migration 0037
# ---------------------------------------------------------------------------

def _install_view(store: Path, version: int) -> None:
    conn = _conn(store)
    try:
        conn.execute("DROP VIEW IF EXISTS deliverables")
        conn.execute(schema_manifest.view_at(version, "deliverables").sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_migration_0037_goes_up_and_down_without_touching_a_row(store):
    db = store / tracks_lib.DB_FILENAME
    _install_view(store, 36)
    base = _set_state(store, _add(store), "failed_delivery")
    round_id = _add_round(store, base, 3)
    _sql(store, "UPDATE dispatches SET state = 'expired', metadata_json = ? WHERE dispatch_id = ?",
         (json.dumps({"deliverable": True, "superseded_by": round_id}), base))
    rows_before = _sql(store, "SELECT * FROM dispatches ORDER BY dispatch_id")

    assert _view(store, f"pr:{base}") == "failed"  # v36 knows no superseded base

    assert apply_0037.apply_migration(db, SQL_0037) is True
    assert _user_version(store) == schema_manifest.TERMINAL_VERSION == 37
    assert _view(store, f"pr:{base}") == "done"
    assert _sql(store, "SELECT * FROM dispatches ORDER BY dispatch_id") == rows_before
    assert apply_0037.apply_migration(db, SQL_0037) is False

    assert apply_0037.revert_migration(db) is True
    assert _user_version(store) == 36
    assert _view(store, f"pr:{base}") == "failed"
    assert _sql(store, "SELECT * FROM dispatches ORDER BY dispatch_id") == rows_before
    assert apply_0037.revert_migration(db) is False
    assert not (store / "t0_receipts.ndjson").exists()  # no row changed: ADR-005 books nothing


def test_migration_0037_down_refuses_a_store_that_is_neither_36_nor_37(store):
    db = store / tracks_lib.DB_FILENAME
    _install_view(store, 35)
    with pytest.raises(apply_0037.Migration0037Error, match="found 35"):
        apply_0037.revert_migration(db)
    assert _user_version(store) == 35


@pytest.mark.parametrize("state,expected", [("failed_delivery", "failed"), ("timed_out", "failed"),
                                            ("dead_letter", "failed"), ("expired", "failed"),
                                            ("withdrawn", "withdrawn"), ("ready", "ready"),
                                            ("proposed", "proposed"), ("running", "in_progress")])
def test_the_v37_view_derives_what_v36_derives_without_a_superseded_row(store, state, expected):
    dlv = _set_state(store, _add(store), state)
    _install_view(store, 36)
    old = _view(store, f"pr:{dlv}")
    _install_view(store, 37)
    assert (_view(store, f"pr:{dlv}"), old) == (expected, expected)


@pytest.mark.parametrize("metadata", [None, "{}", "not json", '{"superseded_by": null}',
                                      '{"superseded_by": 42}', '{"superseded_by": "%s-r3"}'])
def test_a_superseded_base_without_readable_evidence_is_never_done(store, metadata):
    dlv = _set_state(store, _add(store), "expired")
    _add_round(store, dlv, 3)
    _sql(store, "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ?",
         (metadata % dlv if metadata and "%s" in metadata else metadata, dlv))
    # the row does name its round only in the last case; every other blob is no evidence
    expected = "done" if metadata and "%s" in metadata else "failed"
    assert _view(store, f"pr:{dlv}") == expected


def test_the_view_ignores_a_superseded_round_that_is_not_a_round_of_that_id(store):
    dlv = _set_state(store, _add(store), "expired")
    _add_round(store, dlv, 3)
    _sql(store, "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ?",
         (json.dumps({"superseded_by": f"{dlv}-other"}), dlv))
    assert _view(store, f"pr:{dlv}") == "failed"
