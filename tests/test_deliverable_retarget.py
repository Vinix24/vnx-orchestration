"""`vnx deliverable retarget` (track deliverable-retarget-withdraw, PR-1): real CLI, real store."""

from __future__ import annotations

import argparse
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
import planning_cli
import tracks as tracks_lib

from conftest import pin_vnx_data_dir
from fixtures.closure_track_fixture import build_tracks_db

PID, SRC, DST = "test-proj", "feat-source", "feat-target"
INTENT, ABORTED, LEDGER = "deliverable_retarget", "deliverable_retarget_aborted", "t0_receipts.ndjson"


def _track(state_dir: Path, track_id: str, phase: str = "queued") -> None:
    tracks_lib.create_track(state_dir, track_id, PID, track_id, f"ship {track_id}", phase=phase,
                            metadata_json=json.dumps({"plan_scope": "light"}))


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pin_vnx_data_dir(monkeypatch, tmp_path / "data")  # the receipt ledger's store is this state dir
    state_dir = tmp_path / "data" / "state"
    build_tracks_db(state_dir)
    _track(state_dir, SRC)
    _track(state_dir, DST)
    return state_dir


def _ledger(store: Path) -> list[dict]:
    """The retarget lines in the store's receipt ledger, in written order."""
    path = store / LEDGER
    if not path.is_file():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in lines if r.get("trigger") in (INTENT, ABORTED)]


def _cli(store: Path, *argv: str, pid: str = PID) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main([*argv, "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _retarget(store: Path, dlv: str, target: str = DST, *extra: str, reason: str = "wrong track", pid: str = PID):
    return _cli(store, "deliverable", "retarget", dlv, "--objective", target, "--reason", reason, *extra, pid=pid)


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
                       "AND event_type = 'deliverable_retargeted'", (dlv,))


def _add(store: Path, track_id: str = SRC, promote: bool = False) -> str:
    rc, out, err = _cli(store, "deliverable", "add", "--objective", track_id, "--output-kind", "pr", "--title", "t")
    assert rc == 0, err
    dlv = out.splitlines()[0].split(": ", 1)[1]
    if promote:
        assert _cli(store, "deliverable", "promote", dlv)[0] == 0
    return dlv


def _listed(store: Path, track_id: str) -> set[str]:
    rc, out, err = _cli(store, "deliverable", "list", "--objective", track_id, "--json")
    assert rc == 0, err
    return {r["deliverable_ref"] for r in json.loads(out)}


def test_proposed_move_changes_only_the_track_and_writes_one_event(store):
    dlv = _add(store)
    before = _row(store, dlv)
    rc, out, err = _retarget(store, dlv, DST, "--json", reason="belongs on the target")
    assert rc == 0, err
    assert json.loads(out) == {
        "deliverable": dlv, "project_id": PID, "from_track": SRC, "to_track": DST, "prior_state": "proposed",
        "state": "proposed", "reason": "belongs on the target", "action": "retargeted", "applied": True,
    }
    assert {**_row(store, dlv), "updated_at": None} == {**before, "track": DST, "updated_at": None}
    [ev] = _events(store, dlv)
    assert (ev["from_state"], ev["to_state"], ev["actor"], ev["reason"], ev["project_id"]) == \
        ("proposed", "proposed", "operator", "belongs on the target", PID)
    assert json.loads(ev["metadata_json"]) == {"from_track": SRC, "to_track": DST, "prior_state": "proposed"}


def test_ready_move_goes_back_to_proposed_and_says_promote_again(store):
    dlv = _add(store, promote=True)
    rc, out, err = _retarget(store, dlv)
    assert rc == 0, err
    assert "ready -> proposed" in out and f"vnx deliverable promote {dlv}" in out
    row = _row(store, dlv)
    assert (row["track"], row["state"], row["operator_approved_at"], row["output_ref"]) == \
        (DST, "proposed", None, f"pr:{dlv}")
    [ev] = _events(store, dlv)
    assert (ev["from_state"], json.loads(ev["metadata_json"])["prior_state"]) == ("ready", "ready")


def test_parked_target_with_include_parked_lists_the_row_there_and_not_on_the_source(store):
    _track(store, "t-park", "parked")
    dlv = _add(store)
    assert _retarget(store, dlv, "t-park", "--include-parked")[0] == 0
    assert f"pr:{dlv}" in _listed(store, "t-park") and f"pr:{dlv}" not in _listed(store, SRC)
    assert len(_events(store, dlv)) == 1


def _not_a_deliverable(store, output_ref, meta):
    _sql(store, "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, metadata_json) "
                "VALUES ('plain', ?, 'proposed', ?, ?, ?)", (PID, SRC, output_ref, meta))
    return "plain"


def _started(store, state):
    dlv = _add(store)
    _sql(store, "UPDATE dispatches SET state = ? WHERE dispatch_id = ?", (state, dlv))
    return dlv


_REFUSALS = {
    "empty project": (lambda s: _add(s), {"pid": ""}, "ADR-007"),
    "empty reason": (lambda s: _add(s), {"reason": ""}, "--reason"),
    "blank reason": (lambda s: _add(s), {"reason": "   "}, "--reason"),
    "unknown id": (lambda s: "dlv-nope", {}, "not found"),
    "no output_ref": (lambda s: _not_a_deliverable(s, None, '{"deliverable": true}'), {}, "not a deliverable"),
    "empty output_ref": (lambda s: _not_a_deliverable(s, "", '{"deliverable": true}'), {}, "not a deliverable"),
    "door dispatch": (lambda s: _not_a_deliverable(s, "pr:x", '{"title": "x"}'), {}, "not a deliverable"),
    "bad metadata": (lambda s: _not_a_deliverable(s, "pr:x", "not json"), {}, "not a deliverable"),
    "unknown target": (lambda s: _add(s), {"target": "feat-nowhere"}, "not found"),
    "current track": (lambda s: _add(s), {"target": SRC}, "already on track"),
    "done target": (lambda s: (_track(s, "t-done", "done"), _add(s))[1],
                    {"target": "t-done", "extra": ("--include-parked",)}, "phase 'done'"),
    "parked target": (lambda s: (_track(s, "t-park", "parked"), _add(s))[1], {"target": "t-park"}, "--include-parked"),
    "no events table": (lambda s: (_add(s), _sql(s, "DROP TABLE coordination_events"))[0], {}, "coordination_events"),
    **{f"state {st}": (lambda s, st=st: _started(s, st), {}, f"state {st!r}") for st in (
        "active", "completed", "failed", "queued", "claimed", "delivering", "accepted", "running",
        "timed_out", "failed_delivery", "expired", "recovered", "dead_letter")},
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_refusal_is_exit_2_with_no_change_and_no_event(store, case):
    setup, opts, needle = _REFUSALS[case]
    dlv = setup(store)
    before = _row(store, dlv)
    rc, _, err = _retarget(store, dlv, opts.get("target", DST), *opts.get("extra", ()),
                           reason=opts.get("reason", "move"), pid=opts.get("pid", PID))
    assert rc == 2, err
    assert needle in err and "No change made." in err
    assert _row(store, dlv) == before and _events(store, dlv) == [] and _ledger(store) == []


@pytest.mark.parametrize("state", [None, "", "weird", "Proposed"])
def test_a_state_the_store_check_keeps_out_never_reads_as_movable(state):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE dispatches (dispatch_id, project_id, state, track, output_ref, metadata_json)")
    conn.execute("INSERT INTO dispatches VALUES ('d', ?, ?, ?, 'pr:d', '{\"deliverable\": true}')", (PID, state, SRC))
    args = argparse.Namespace(dispatch_id="d", objective=DST, include_parked=True)
    with pytest.raises(planning_cli._RetargetRefused, match="only proposed or ready"):
        planning_cli._retarget_check(conn, Path("/nonexistent"), PID, args)


@pytest.mark.parametrize("phase", [None, "", "archived", 7])
def test_a_target_phase_that_is_not_open_is_refused(store, monkeypatch, phase):
    dlv = _add(store)
    real = tracks_lib.get_track
    monkeypatch.setattr(tracks_lib, "get_track", lambda sd, t, p: {**real(sd, t, p), "phase": phase})
    rc, _, err = _retarget(store, dlv, DST, "--include-parked")
    assert rc == 2 and "phase" in err and _row(store, dlv)["track"] == SRC and _events(store, dlv) == []


@pytest.mark.parametrize("change, seen", [("track = 't-third'", "'t-third'"), ("state = 'ready'", "'ready'")])
def test_a_row_that_changes_between_read_and_write_is_exit_1_with_no_event(store, monkeypatch, change, seen):
    dlv = _add(store)
    real = tracks_lib.get_track
    sql = f"UPDATE dispatches SET {change} WHERE dispatch_id = ?"
    monkeypatch.setattr(tracks_lib, "get_track", lambda sd, t, p: (_sql(store, sql, (dlv,)), real(sd, t, p))[1])
    rc, _, err = _retarget(store, dlv)
    assert rc == 1 and seen in err and "No change made." in err
    assert _row(store, dlv)["track"] != DST and _events(store, dlv) == []
    now = _row(store, dlv)
    intent, aborted = _ledger(store)
    assert (intent["trigger"], aborted["trigger"]) == (INTENT, ABORTED)
    assert (aborted["state"], aborted["track"], aborted["abort_cause"]) == (now["state"], now["track"], "row_changed")


def test_an_event_insert_that_raises_rolls_the_move_back(store, monkeypatch):
    dlv = _add(store, promote=True)
    before = _row(store, dlv)

    def _broken(*_a, **_k):
        raise sqlite3.OperationalError("injected: event insert fails")

    monkeypatch.setattr(planning_cli, "_append_coordination_event", _broken)
    rc, _, err = _retarget(store, dlv)
    assert rc == 1 and "injected: event insert fails" in err and "No change made." in err
    assert _row(store, dlv) == before and _events(store, dlv) == []
    intent, aborted = _ledger(store)
    assert (intent["trigger"], aborted["trigger"], aborted["retarget_id"]) == (INTENT, ABORTED, intent["retarget_id"])
    assert (aborted["state"], aborted["track"]) == ("ready", SRC)
    assert aborted["abort_cause"] == "store_error: injected: event insert fails"


_INTENT_FIELDS = ("event_type", "receipt_kind", "terminal", "source", "trigger", "dispatch_id", "project_id",
                  "from_track", "to_track", "prior_state", "to_state", "actor", "reason")


def test_the_decision_lands_in_the_ledger_before_the_row_moves(store, monkeypatch):
    dlv = _add(store, promote=True)
    real, seen = append_receipt.append_receipt_payload, []

    def _observing(receipt, **kwargs):
        row = _row(store, dlv)
        seen.append((row["track"], row["state"], len(_events(store, dlv))))
        return real(receipt, **kwargs)

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _observing)
    rc, _, err = _retarget(store, dlv, reason="belongs on the target")
    assert rc == 0, err
    lines = _ledger(store)
    assert len(lines) == 1, lines
    [line] = lines
    assert {k: line[k] for k in _INTENT_FIELDS} == {
        "event_type": "state_mutation", "receipt_kind": "state_mutation", "terminal": "T0",
        "source": "vnx_planning", "trigger": INTENT, "dispatch_id": dlv, "project_id": PID, "from_track": SRC,
        "to_track": DST, "prior_state": "ready", "to_state": "proposed", "actor": "operator",
        "reason": "belongs on the target",
    }
    assert line["timestamp"] and line["status"] == f"{INTENT}:{line['retarget_id']}"
    assert seen == [(SRC, "ready", 0)]
    assert (_row(store, dlv)["track"], len(_events(store, dlv))) == (DST, 1)


def _ledger_is_a_directory(store, monkeypatch):
    (store / LEDGER).mkdir()


def _writer_returns(value):
    return lambda store, monkeypatch: monkeypatch.setattr(append_receipt, "append_receipt_payload",
                                                          lambda receipt, **kwargs: value)


def _writer_raises(store, monkeypatch):
    def _raise(receipt, **kwargs):
        raise append_receipt.AppendReceiptError("lock_failed", 13, "injected: lock taken")
    monkeypatch.setattr(append_receipt, "append_receipt_payload", _raise)


def _result(status):
    return append_receipt.AppendResult(status=status, receipts_file=Path("x"), idempotency_key="k")


_LEDGER_FAILURES = {
    "ledger path is a directory": _ledger_is_a_directory,
    "writer raises": _writer_raises,
    "no result": _writer_returns(None),
    "result without a status": _writer_returns(object()),
    "duplicate": _writer_returns(_result("duplicate")),
    "empty status": _writer_returns(_result("")),
    "unknown status": _writer_returns(_result("APPENDED")),
}


@pytest.mark.parametrize("case", sorted(_LEDGER_FAILURES))
def test_a_ledger_that_does_not_take_the_line_is_exit_1_and_nothing_moves(store, monkeypatch, case):
    dlv = _add(store, promote=True)
    before = _row(store, dlv)
    _LEDGER_FAILURES[case](store, monkeypatch)
    rc, _, err = _retarget(store, dlv)
    assert rc == 1, err
    assert f"the {INTENT} receipt was not written to the ledger" in err and "No change made." in err
    after = _row(store, dlv)
    assert (after["track"], after["state"]) == (SRC, "ready") and before["operator_approved_at"]
    assert after == before  # operator_approved_at included
    assert _events(store, dlv) == [] and _ledger(store) == []


def _cas_lost_after_the_intent(store, monkeypatch, dlv, abort_writer=None):
    """The real writer appends the intent line, then the row moves to 't-third' before the swap."""
    real = append_receipt.append_receipt_payload

    def _then_move(receipt, **kwargs):
        if receipt["trigger"] == ABORTED and abort_writer is not None:
            return abort_writer(receipt, **kwargs)
        result = real(receipt, **kwargs)
        if receipt["trigger"] == INTENT:
            _sql(store, "UPDATE dispatches SET track = 't-third', state = 'ready' WHERE dispatch_id = ?", (dlv,))
        return result

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _then_move)


def test_a_cas_lost_after_the_ledger_line_writes_the_aborted_line(store, monkeypatch):
    dlv = _add(store)
    _cas_lost_after_the_intent(store, monkeypatch, dlv)
    rc, _, err = _retarget(store, dlv)
    assert rc == 1 and "changed while this command ran" in err and "'t-third'" in err
    intent, aborted = _ledger(store)
    assert (intent["trigger"], intent["from_track"], intent["to_track"]) == (INTENT, SRC, DST)
    assert (aborted["trigger"], aborted["dispatch_id"], aborted["retarget_id"]) == \
        (ABORTED, dlv, intent["retarget_id"])
    assert (aborted["state"], aborted["track"], aborted["abort_cause"]) == ("ready", "t-third", "row_changed")
    assert aborted["status"] != intent["status"] and _events(store, dlv) == []


def test_an_aborted_line_that_fails_is_reported_and_keeps_exit_1(store, monkeypatch):
    dlv = _add(store)

    def _broken(receipt, **kwargs):
        raise append_receipt.AppendReceiptError("receipt_write_failed", 12, "injected: disk full")

    _cas_lost_after_the_intent(store, monkeypatch, dlv, abort_writer=_broken)
    rc, _, err = _retarget(store, dlv)
    assert rc == 1 and "changed while this command ran" in err
    assert f"the {ABORTED} receipt was not written to the ledger" in err and "injected: disk full" in err
    assert "without its abort" in err
    assert [line["trigger"] for line in _ledger(store)] == [INTENT]


def test_a_second_move_of_the_same_deliverable_gets_its_own_ledger_line(store):
    _track(store, "t-third")
    dlv = _add(store)
    assert _retarget(store, dlv, DST)[0] == 0
    rc, _, err = _retarget(store, dlv, "t-third")
    assert rc == 0, err
    first, second = _ledger(store)
    assert [(r["from_track"], r["to_track"]) for r in (first, second)] == [(SRC, DST), (DST, "t-third")]
    assert first["retarget_id"] != second["retarget_id"]


def test_an_unreadable_row_after_the_intent_is_still_recorded_as_aborted(store):
    conn = sqlite3.connect(":memory:")
    conn.close()
    decision = {"retarget_id": "r1", "dispatch_id": "d", "project_id": PID, "from_track": SRC, "to_track": DST,
                "prior_state": "proposed", "to_state": "proposed", "actor": "operator", "reason": "move"}
    shown = planning_cli._retarget_aborted(conn, store, PID, decision, "row_changed")
    assert shown.startswith("unreadable (")
    [aborted] = _ledger(store)
    assert (aborted["state"], aborted["track"], aborted["trigger"]) == (None, None, ABORTED)
    assert aborted["reread_error"]


def test_fourth_deliverable_raises_the_target_and_keeps_stdout_json(store):
    for _ in range(3):
        _add(store, DST)
    dlv = _add(store)
    rc, out, err = _retarget(store, dlv, DST, "--json")
    assert rc == 0, err
    assert json.loads(out)["to_track"] == DST
    assert f"plan-scope: {DST} light -> architecture" in err
    meta = lambda t: json.loads(tracks_lib.get_track(store, t, PID)["metadata_json"])
    assert (meta(DST)["plan_scope"], meta(SRC)["plan_scope"]) == ("architecture", "light")
    assert _sql(store, "SELECT 1 FROM track_open_items WHERE oi_id = ? AND resolved_at IS NULL", (f"OI-PLAN-{DST}",))
