"""`vnx deliverable withdraw` and the terminal state withdrawn (ADR-049, PR-2): real CLI, real store."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import append_receipt
import coordination_db
import dispatch_cli
import planning_cli
import runtime_supervisor
import track_reconciler
import tracks as tracks_lib

from conftest import pin_vnx_data_dir
from fixtures.closure_track_fixture import build_tracks_db
from fixtures.live_store_fixture import build_live_store, terminal_deliverables_view

PID, SRC = "test-proj", "feat-source"
INTENT, ABORTED, LEDGER = "deliverable_withdraw", "deliverable_withdraw_aborted", "t0_receipts.ndjson"


def _track(state_dir: Path, track_id: str = SRC, phase: str = "queued") -> None:
    tracks_lib.create_track(state_dir, track_id, PID, track_id, f"ship {track_id}", phase=phase,
                            metadata_json=json.dumps({"plan_scope": "light"}))


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pin_vnx_data_dir(monkeypatch, tmp_path / "data")  # the receipt ledger's store is this state dir
    state_dir = tmp_path / "data" / "state"
    build_live_store(state_dir)
    _track(state_dir)
    return state_dir


def _cli(store: Path, *argv: str, pid: str = PID) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main([*argv, "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _withdraw(store: Path, dlv: str, *extra: str, reason: str = "not built any more", pid: str = PID):
    return _cli(store, "deliverable", "withdraw", dlv, "--reason", reason, *extra, pid=pid)


def _sql(store: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.commit()
    finally:
        conn.close()
    return rows


def _row(store: Path, dlv: str) -> dict:
    return (_sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ?", (dlv,)) or [{}])[0]


def _events(store: Path, dlv: str) -> list[dict]:
    if not _sql(store, "SELECT 1 FROM sqlite_master WHERE name = 'coordination_events'"):
        return []
    return _sql(store, "SELECT * FROM coordination_events WHERE entity_id = ? "
                       "AND event_type = 'deliverable_withdrawn'", (dlv,))


def _ledger(store: Path) -> list[dict]:
    path = store / LEDGER
    if not path.is_file():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in lines if r.get("trigger") in (INTENT, ABORTED)]


def _add(store: Path, track_id: str = SRC, promote: bool = False) -> str:
    rc, out, err = _cli(store, "deliverable", "add", "--objective", track_id, "--output-kind", "pr", "--title", "t")
    assert rc == 0, err
    dlv = out.splitlines()[0].split(": ", 1)[1]
    if promote:
        assert _cli(store, "deliverable", "promote", dlv)[0] == 0
    return dlv


def _set_state(store: Path, dlv: str, state: object) -> str:
    _sql(store, "UPDATE dispatches SET state = ? WHERE dispatch_id = ?", (state, dlv))
    return dlv


def _derived(store: Path, track_id: str = SRC) -> str:
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    try:
        return track_reconciler._compute_derived_status(conn, track_id, PID)
    finally:
        conn.close()


# --- the state machine: one canonical terminal set --------------------------

def test_withdrawn_is_a_terminal_state_reached_only_from_proposed_or_ready():
    assert "withdrawn" in coordination_db.DISPATCH_STATES
    assert "withdrawn" in coordination_db.TERMINAL_DISPATCH_STATES
    assert coordination_db.DISPATCH_TRANSITIONS["withdrawn"] == frozenset()
    assert {s for s, nxt in coordination_db.DISPATCH_TRANSITIONS.items() if "withdrawn" in nxt} == {"proposed", "ready"}
    assert "withdrawn" not in coordination_db.IN_FLIGHT_DISPATCH_STATES
    assert "withdrawn" in dispatch_cli._REFIRE_RUNTIME_END_STATES


def test_the_copies_read_the_one_canonical_set():
    assert track_reconciler.TERMINAL_DISPATCH_STATES is coordination_db.TERMINAL_DISPATCH_STATES
    assert runtime_supervisor._TERMINAL_DISPATCH_STATES is coordination_db.TERMINAL_DISPATCH_STATES


# --- the verb -----------------------------------------------------------------

def test_proposed_withdraw_writes_the_state_and_one_event(store):
    dlv = _add(store)
    before = _row(store, dlv)
    rc, out, err = _withdraw(store, dlv, "--json", reason="P22 is optional and not wanted")
    assert rc == 0, err
    assert json.loads(out) == {
        "deliverable": dlv, "project_id": PID, "track": SRC, "prior_state": "proposed", "state": "withdrawn",
        "reason": "P22 is optional and not wanted", "action": "withdrawn", "applied": True,
    }
    assert {**_row(store, dlv), "updated_at": None} == {**before, "state": "withdrawn", "updated_at": None}
    [ev] = _events(store, dlv)
    assert (ev["from_state"], ev["to_state"], ev["actor"], ev["reason"], ev["project_id"]) == \
        ("proposed", "withdrawn", "operator", "P22 is optional and not wanted", PID)
    assert json.loads(ev["metadata_json"]) == {"track": SRC, "prior_state": "proposed"}
    [line] = _ledger(store)
    assert (line["trigger"], line["dispatch_id"], line["prior_state"], line["reason"]) == \
        (INTENT, dlv, "proposed", "P22 is optional and not wanted")
    assert line["status"] == f"{INTENT}:{line['withdraw_id']}"


def test_ready_withdraw_says_what_it_did(store):
    dlv = _add(store, promote=True)
    rc, out, err = _withdraw(store, dlv)
    assert rc == 0, err
    assert f"ready -> withdrawn on {SRC}" in out
    assert _row(store, dlv)["state"] == "withdrawn" and len(_events(store, dlv)) == 1


def _not_a_deliverable(store, output_ref, meta):
    _sql(store, "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, metadata_json) "
                "VALUES ('plain', ?, 'proposed', ?, ?, ?)", (PID, SRC, output_ref, meta))
    return "plain"


def _other_project(store):
    dlv = _add(store)
    _sql(store, "UPDATE dispatches SET project_id = 'other-proj' WHERE dispatch_id = ?", (dlv,))
    return dlv


def _below_v36(store):
    _sql(store, "PRAGMA user_version = 35")
    return _add(store)


def _with_the_0022_check(store):
    (store / tracks_lib.DB_FILENAME).unlink()
    build_tracks_db(store)  # the store `vnx init` builds: dispatches with the 0022 state CHECK
    terminal_deliverables_view(store / tracks_lib.DB_FILENAME)
    _track(store)
    return _add(store)


_REFUSALS = {
    "empty project": (lambda s: _add(s), {"pid": ""}, "ADR-007"),
    "empty reason": (lambda s: _add(s), {"reason": ""}, "--reason"),
    "blank reason": (lambda s: _add(s), {"reason": "  \t "}, "--reason"),
    "unknown id": (lambda s: "dlv-nope", {}, "not found"),
    "other project": (_other_project, {}, "not found"),
    "no output_ref": (lambda s: _not_a_deliverable(s, None, '{"deliverable": true}'), {}, "not a deliverable"),
    "door dispatch": (lambda s: _not_a_deliverable(s, "pr:x", '{"title": "x"}'), {}, "not a deliverable"),
    "bad metadata": (lambda s: _not_a_deliverable(s, "pr:x", "not json"), {}, "not a deliverable"),
    "completed": (lambda s: _set_state(s, _add(s), "completed"), {}, "delivery evidence"),
    "already withdrawn": (lambda s: _set_state(s, _add(s), "withdrawn"), {}, "state 'withdrawn'"),
    "store below v36": (_below_v36, {}, "migration 0036"),
    "store with the 0022 CHECK": (_with_the_0022_check, {}, "CHECK"),
    "no events table": (lambda s: (_add(s), _sql(s, "DROP TABLE coordination_events"))[0], {}, "coordination_events"),
    **{f"state {st!r}": (lambda s, st=st: _set_state(s, _add(s), st), {}, f"state {st!r}") for st in (
        "active", "failed", "queued", "claimed", "delivering", "accepted", "running", "timed_out",
        "failed_delivery", "expired", "recovered", "dead_letter", "", "Proposed", "withdrawn ", "weird")},
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_refusal_is_exit_2_with_no_change_no_event_and_no_ledger_line(store, case):
    setup, opts, needle = _REFUSALS[case]
    dlv = setup(store)
    before = _sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ?", (dlv,))
    rc, _, err = _withdraw(store, dlv, reason=opts.get("reason", "drop it"), pid=opts.get("pid", PID))
    assert rc == 2, err
    assert needle in err and "No change made." in err
    assert _sql(store, "SELECT * FROM dispatches WHERE dispatch_id = ?", (dlv,)) == before
    assert _events(store, dlv) == [] and _ledger(store) == []


def test_a_row_that_changes_after_the_ledger_line_is_exit_1_with_the_aborted_line(store, monkeypatch):
    dlv = _add(store)
    real = append_receipt.append_receipt_payload

    def _then_promote(receipt, **kwargs):
        result = real(receipt, **kwargs)
        if receipt["trigger"] == INTENT:
            _set_state(store, dlv, "ready")
        return result

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _then_promote)
    rc, _, err = _withdraw(store, dlv)
    assert rc == 1 and "changed while this command ran" in err and "'ready'" in err
    assert _row(store, dlv)["state"] == "ready" and _events(store, dlv) == []
    intent, aborted = _ledger(store)
    assert (intent["trigger"], aborted["trigger"], aborted["withdraw_id"]) == (INTENT, ABORTED, intent["withdraw_id"])
    assert (aborted["state"], aborted["abort_cause"]) == ("ready", "row_changed")


def test_a_retarget_between_the_read_and_the_swap_leaves_the_moved_row_alone(store, monkeypatch):
    dst = "feat-destination"
    _track(store, dst)
    dlv = _add(store)
    real = planning_cli._withdraw_check

    def _then_retarget(conn, project_id, dispatch_id):
        row = real(conn, project_id, dispatch_id)  # withdraw read the row on SRC
        rc, _, err = _cli(store, "deliverable", "retarget", dispatch_id, "--objective", dst, "--reason", "moves first")
        assert rc == 0, err
        return row

    monkeypatch.setattr(planning_cli, "_withdraw_check", _then_retarget)
    rc, _, err = _withdraw(store, dlv)
    assert rc == 1 and "changed while this command ran" in err and f"'proposed' on {SRC!r}" in err
    assert (_row(store, dlv)["state"], _row(store, dlv)["track"]) == ("proposed", dst)
    assert _events(store, dlv) == []
    intent, aborted = _ledger(store)
    assert (intent["trigger"], intent["track"], aborted["trigger"]) == (INTENT, SRC, ABORTED)
    assert (aborted["withdraw_id"], aborted["state"], aborted["track"], aborted["abort_cause"]) == \
        (intent["withdraw_id"], "proposed", dst, "row_changed")


@pytest.mark.parametrize("error, rc", [(sqlite3.OperationalError("injected: event insert fails"), 1),
                                       (RuntimeError("injected: not a store error"), None)])
def test_an_event_insert_that_raises_rolls_back_and_books_the_aborted_line(store, monkeypatch, error, rc):
    dlv = _add(store, promote=True)
    before = _row(store, dlv)

    def _broken(*_a, **_k):
        raise error

    monkeypatch.setattr(planning_cli, "_append_coordination_event", _broken)
    if rc is None:
        with pytest.raises(RuntimeError, match="not a store error"):
            _withdraw(store, dlv)
    else:
        assert _withdraw(store, dlv)[0] == rc
    assert _row(store, dlv) == before and _events(store, dlv) == []
    intent, aborted = _ledger(store)
    assert (intent["trigger"], aborted["trigger"], aborted["state"]) == (INTENT, ABORTED, "ready")
    assert str(error) in aborted["abort_cause"]


def test_a_ledger_that_refuses_the_line_is_exit_1_and_nothing_changes(store, monkeypatch):
    dlv = _add(store)
    before = _row(store, dlv)
    monkeypatch.setattr(append_receipt, "append_receipt_payload", lambda receipt, **kwargs: None)
    rc, _, err = _withdraw(store, dlv)
    assert rc == 1 and f"the {INTENT} receipt was not written to the ledger" in err
    assert _row(store, dlv) == before and _events(store, dlv) == []


def test_close_and_promote_name_the_withdrawn_state(store):
    dlv = _set_state(store, _add(store), "withdrawn")
    rc, _, err = _cli(store, "deliverable", "close", dlv, "--evidence", "#12")
    assert rc == 2 and "found 'withdrawn'" in err
    rc, _, err = _cli(store, "deliverable", "promote", dlv)
    assert rc == 1 and "found 'withdrawn'" in err
    assert _row(store, dlv)["state"] == "withdrawn"


# --- the readers -------------------------------------------------------------

def test_the_view_returns_withdrawn_for_a_withdrawn_row(store):
    dlv = _set_state(store, _add(store), "withdrawn")
    [view] = _sql(store, "SELECT * FROM deliverables WHERE deliverable_ref = ?", (f"pr:{dlv}",))
    assert view["derived_status"] == "withdrawn"
    assert (view["withdrawn_count"], view["planned_count"], view["completed_count"], view["in_flight_count"]) == \
        (1, 0, 0, 0)


def test_a_track_with_one_withdrawn_and_one_delivered_deliverable_reconciles_to_done(store):
    _set_state(store, _add(store), "withdrawn")
    _set_state(store, _add(store), "completed")
    assert _derived(store) == "done"


@pytest.mark.parametrize("state", ["", "Withdrawn", "withdrawn ", "weird"])  # NULL: the store refuses it
def test_a_state_that_is_not_exactly_withdrawn_is_never_left_out(store, state):
    _set_state(store, _add(store), "completed")
    _set_state(store, _add(store), state)
    assert _derived(store) != "done"


def test_a_track_with_only_withdrawn_deliverables_is_not_done(store):
    _set_state(store, _add(store), "withdrawn")
    assert _derived(store) == "queued"  # the declared phase decides, not the withdrawn row


def test_a_done_track_never_ends_a_withdrawn_deliverable(store):
    withdrawn, ready = _set_state(store, _add(store), "withdrawn"), _add(store, promote=True)
    _sql(store, "UPDATE tracks SET phase = 'done' WHERE track_id = ?", (SRC,))
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    try:
        assert track_reconciler._reconcile_deliverable_dispatches(conn, SRC, PID) == 1
        conn.commit()
    finally:
        conn.close()
    assert (_row(store, withdrawn)["state"], _row(store, ready)["state"]) == ("withdrawn", "completed")


def _listed(store: Path, *extra: str) -> set[str]:
    rc, out, err = _cli(store, "deliverable", "list", "--objective", SRC, "--json", *extra)
    assert rc == 0, err
    return {r["deliverable_ref"] for r in json.loads(out)}


def test_list_hides_withdrawn_unless_all(store):
    gone, kept = _add(store), _add(store)
    assert _withdraw(store, gone)[0] == 0
    assert _listed(store) == {f"pr:{kept}"}
    assert _listed(store, "--all") == {f"pr:{kept}", f"pr:{gone}"}
    rc, out, _ = _cli(store, "deliverable", "list", "--objective", SRC)
    assert rc == 0 and "(1 withdrawn hidden — --all to show)" in out and f"pr:{gone}" not in out


def test_queue_hides_withdrawn_unless_all(store):
    gone, kept = _add(store), _add(store)
    assert _withdraw(store, gone)[0] == 0

    def _queued(*extra):
        rc, out, err = _cli(store, "objective", "queue", "--objective", SRC, "--json", *extra)
        assert rc == 0, err
        return {(r["deliverable"], r["state"]) for r in json.loads(out)}

    assert _queued() == {(kept, "proposed")}
    assert _queued("--all") == {(kept, "proposed"), (gone, "withdrawn")}


def test_scope_count_and_plan_gate_paths_leave_a_withdrawn_deliverable_out(store):
    gone = _add(store)
    assert _cli(store, "deliverable", "set", gone, "--dispatch-paths", "scripts/gone.py")[0] == 0
    assert _withdraw(store, gone)[0] == 0
    for _ in range(3):
        _add(store)
    assert len(planning_cli._fetch_deliverable_records(store, PID, SRC)) == 3
    assert planning_cli._deliverable_dispatch_paths(store, PID, SRC) == []
    assert json.loads(tracks_lib.get_track(store, SRC, PID)["metadata_json"])["plan_scope"] == "light"
