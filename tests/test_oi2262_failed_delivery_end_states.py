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


def _add_round(store: Path, base: str, number: Any = 3, state: str = "completed",
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


def test_close_of_a_failed_base_is_refused_when_the_completed_row_is_not_a_round(store):
    """OI-2403: a completed dispatch that is not ``<base>-rK`` is not this base's delivery."""
    dlv = _set_state(store, _add(store), "failed_delivery")
    _set_state(store, _add(store), "completed")  # an unrelated completed dispatch
    before = _row(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []
    assert _view(store, f"pr:{dlv}") == "failed"


@pytest.mark.parametrize("round_label", ["0", "03", "X", ""])
def test_close_of_a_failed_base_is_refused_when_its_round_is_not_a_positive_integer(store, round_label):
    """OI-2403: ``-r0``, ``-r03``, ``-rX`` and a bare ``-r`` are not rounds of the base."""
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, round_label)
    before = _row(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "No change made." in err
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []
    assert _view(store, f"pr:{dlv}") == "failed"


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


@pytest.mark.parametrize("round_label", ["0", "03", "X", ""])
def test_the_view_ignores_a_completed_target_whose_suffix_is_not_a_positive_integer(store, round_label):
    """OI-2403: the target row exists and is ``completed``, but it is not a ``<base>-rK`` round."""
    dlv = _set_state(store, _add(store), "expired")
    round_id = _add_round(store, dlv, round_label)
    _sql(store, "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ?",
         (json.dumps({"superseded_by": round_id}), dlv))

    assert _view(store, f"pr:{dlv}") == "failed"


def test_the_view_ignores_a_superseded_target_that_is_an_unrelated_completed_dispatch(store):
    """Codex round 3 reproduction: any completed dispatch of the project used to derive ``done``."""
    dlv = _set_state(store, _add(store), "expired")
    unrelated = _set_state(store, _add(store), "completed")
    _sql(store, "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ?",
         (json.dumps({"superseded_by": unrelated}), dlv))

    assert unrelated != dlv
    assert _view(store, f"pr:{dlv}") == "failed"


# ---------------------------------------------------------------------------
# 3b. The round id is validated by one helper the view's SQL mirrors (OI-2403)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("round_id,expected", [
    ("dlv-abc-r1", True), ("dlv-abc-r3", True), ("dlv-abc-r27", True),
    ("dlv-abc-r0", False), ("dlv-abc-r03", False), ("dlv-abc-r", False),
    ("dlv-abc-rX", False), ("dlv-abc-r3x", False), ("dlv-abc-rx3", False),
    ("dlv-abc-r-1", False), ("dlv-abc-r3-r4", False), ("dlv-abc-r 3", False),
    ("dlv-abc-r٣", False), ("dlv-abc", False), ("dlv-abc-r3-", False),
    ("dlv-abd-r3", False), ("", False), (None, False), (42, False),
])
def test_is_round_of_accepts_only_a_positive_integer_round(round_id, expected):
    assert schema_manifest.is_round_of("dlv-abc", round_id) is expected


def test_the_view_and_the_helper_agree_on_every_round_shape(store):
    """The view's SQL is a mirror of ``is_round_of``; this pins the two together."""
    shapes = [("dlv-abc-r1", True), ("dlv-abc-r0", False), ("dlv-abc-r03", False),
              ("dlv-abc-r", False), ("dlv-abc-rX", False)]
    for round_id, expected in shapes:
        assert schema_manifest.is_round_of("dlv-abc", round_id) is expected
    base = _set_state(store, _add(store), "expired")
    for label, expected in [("1", "done"), ("0", "failed"), ("03", "failed"),
                            ("", "failed"), ("X", "failed")]:
        _sql(store, "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ?",
             (json.dumps({"superseded_by": f"{base}-r{label}"}), base))
        _add_round(store, base, label)
        assert _view(store, f"pr:{base}") == expected, label


# ---------------------------------------------------------------------------
# 4. The supersede compare-and-swap protects metadata_json, not only the state
# ---------------------------------------------------------------------------

class _LoadsHook:
    """``planning_cli``'s ``json`` namespace with a one-shot pause on its first ``loads``.

    The close parses the metadata it read with ``json.loads`` immediately before it writes
    the row, so that call is the seam a test can pause on — no sleep, no thread. Everything
    else on the namespace (``dumps``, ``load``, ``dump``) is the real ``json`` module.
    """

    def __init__(self, pause) -> None:
        self._pause = pause
        self.fired = False

    def loads(self, value, *args, **kwargs):
        parsed = json.loads(value, *args, **kwargs)
        if not self.fired:
            self.fired = True
            self._pause()
        return parsed

    def __getattr__(self, name):
        return getattr(json, name)


def _competing_metadata_write(store: Path, dispatch_id: str, key: str, value: Any) -> bool:
    """Connection B: merge one key into the base row's metadata; True if it committed.

    ``timeout=0`` so the attempt is refused at once when A holds the write lock instead of
    blocking the close that is waiting for this very call (one thread, two connections).
    """
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME), timeout=0)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, PID),
        ).fetchone()
        metadata = json.loads((row[0] if row else None) or "{}")
        metadata[key] = value
        conn.execute(
            "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ? AND project_id = ?",
            (json.dumps(metadata), dispatch_id, PID),
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        conn.rollback()
        return False
    finally:
        conn.close()


def test_a_metadata_write_between_the_read_and_the_write_is_not_lost(store, monkeypatch):
    """Codex's two-connection reproduction: B's key survives the close, in either interleaving."""
    dlv = _set_state(store, _add(store), "failed_delivery")
    round_id = _add_round(store, dlv, 3)
    committed: List[bool] = []
    hook = _LoadsHook(lambda: committed.append(_competing_metadata_write(store, dlv, "operator_note", "kept")))
    monkeypatch.setattr(planning_cli, "json", hook)

    rc, out, err = _close(store, dlv)

    assert hook.fired, "the pause did not land between the read and the write"
    assert rc == 0, err
    if committed[0] is False:
        # A held the write lock, so B's write was refused rather than lost: a client that
        # retries lands its key on the metadata A committed.
        assert _competing_metadata_write(store, dlv, "operator_note", "kept") is True
    metadata = json.loads(_row(store, dlv)["metadata_json"])
    assert metadata.get("operator_note") == "kept"
    assert metadata["superseded_by"] == round_id


def test_a_metadata_write_committed_before_the_close_is_merged_not_replaced(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    round_id = _add_round(store, dlv, 3)
    assert _competing_metadata_write(store, dlv, "operator_note", "kept") is True

    rc, _, err = _close(store, dlv)

    assert rc == 0, err
    metadata = json.loads(_row(store, dlv)["metadata_json"])
    assert (metadata.get("operator_note"), metadata["superseded_by"]) == ("kept", round_id)


def test_a_stale_metadata_read_misses_the_compare_and_swap_and_writes_nothing(store, monkeypatch):
    """A read taken before the write transaction is caught instead of overwriting B's key."""
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3)
    stale = _row(store, dlv)["metadata_json"]
    assert _competing_metadata_write(store, dlv, "operator_note", "kept") is True
    real_read = planning_cli._read_superseded_row

    def stale_read(conn, project_id, dispatch_id):
        row = real_read(conn, project_id, dispatch_id)
        return {"state": row["state"], "metadata_json": stale}

    monkeypatch.setattr(planning_cli, "_read_superseded_row", stale_read)
    before, events_before = _row(store, dlv), _events(store, dlv)

    rc, _, err = _close(store, dlv)

    assert rc == 2 and "changed under the read" in err and "No change made." in err
    assert _row(store, dlv) == before and _events(store, dlv) == events_before
    assert json.loads(_row(store, dlv)["metadata_json"])["operator_note"] == "kept"


def test_the_supersede_write_still_binds_the_state_it_was_read_in(store):
    dlv = _set_state(store, _add(store), "failed_delivery")
    _add_round(store, dlv, 3)
    before = _row(store, dlv)
    conn = _conn(store)
    try:
        outcome = planning_cli._close_superseded_attempt(
            conn, project_id=PID, dispatch_id=dlv, from_state="timed_out",
            pr_ref="#2262", evidence_number=2262,
        )
    finally:
        conn.close()

    assert outcome == {"action": "row_changed"}
    assert _row(store, dlv) == before
    assert _events(store, dlv, "deliverable_superseded") == []
