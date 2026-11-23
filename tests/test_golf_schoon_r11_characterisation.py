"""Characterisation net for golf-schoon R11: ``_close_with_attest``
(``scripts/planning_cli.py``) keeps its exact exit codes, stdout/stderr lines,
written rows and side-effect order across the extract-method cut. Real temp
stores; only the date and, per case, a failing collaborator are patched.
"""
from __future__ import annotations

import json

import pytest

# The sibling test module puts scripts/ and scripts/lib/ on sys.path first.
from test_planning_cli import PROJECT_ID, _build_db, _close_args, _phase, _pr_ref, _track_events
import planning_cli
import tracks as tracks_lib
import track_reconciler

_HEAD = "\nvnx objective close --attest — ops (project 'test-proj')\n"


class _Day:
    @staticmethod
    def today():
        import datetime
        return datetime.date(2026, 11, 14)


@pytest.fixture
def sd(tmp_path, monkeypatch):
    monkeypatch.setattr(planning_cli, "date", _Day)
    store = _build_db(tmp_path)
    tracks_lib.create_track(store, "ops", PROJECT_ID, title="t", goal_state="g", phase="queued")
    return store


def _attest(sd, capsys, **kw):
    kw.setdefault("apply", True)
    kw.setdefault("approval_id", "APR")
    rc = planning_cli._close_with_attest(
        _close_args(sd, "ops", attest="why", **kw), sd, "ops", PROJECT_ID, "why", None)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.mark.parametrize("kw, rc, err", [
    ({"apply": False}, 2, "objective close --attest: --apply is required to write an "
                          "attestation. No change made.\n"),
    ({"approval_id": "  "}, 2, "objective close --attest: --approval-id is required (the "
                               "operator's gate token). No change made.\n"),
])
def test_attest_gate_refusals(sd, capsys, kw, rc, err):
    assert _attest(sd, capsys, **kw) == (rc, "", err)
    assert _phase(sd, "ops") == "queued" and _pr_ref(sd, "ops") == ""


def test_attest_track_not_found(sd, capsys):
    rc = planning_cli._close_with_attest(
        _close_args(sd, "nope", apply=True, approval_id="A"), sd, "nope", PROJECT_ID, "r", None)
    assert rc == 1
    assert capsys.readouterr().err == ("objective close --attest: track not found: 'nope' "
                                       "(project 'test-proj'). No change made.\n")


def _set_phase(sd, phase):
    import sqlite3
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("UPDATE tracks SET phase = ? WHERE track_id = 'ops'", (phase,))
    conn.commit()
    conn.close()


def _payload(declared, action, **extra):
    base = {"track_id": "ops", "project_id": PROJECT_ID, "declared_phase": declared,
            "action": action, "applied": False}
    return json.dumps({**base, **extra}, indent=2, default=str) + "\n"


def test_attest_noop_parked_nopath_and_invalid_pr(sd, capsys, monkeypatch):
    _set_phase(sd, "done")
    assert _attest(sd, capsys) == (0, _HEAD + "\n  already closed (phase=done).\n\n", "")
    assert _attest(sd, capsys, json=True) == (0, _payload("done", "noop_already_closed"), "")
    _set_phase(sd, "parked")
    assert _attest(sd, capsys) == (2, _HEAD + "\n  track is PARKED (a deliberate stop). Pass "
                                   "--include-parked to close it anyway, or un-park it first. "
                                   "No change made.\n\n", "")
    assert _attest(sd, capsys, json=True) == (2, _payload("parked", "rejected_parked"), "")
    _set_phase(sd, "queued")
    monkeypatch.setattr(planning_cli, "_phase_path_to", lambda a, b: None)
    assert _attest(sd, capsys) == (2, _HEAD + "\n  ERROR: no legal transition path queued -> "
                                   "done (ALLOWED_TRANSITIONS). No change made.\n\n", "")
    monkeypatch.undo()
    monkeypatch.setattr(planning_cli, "date", _Day)
    assert _attest(sd, capsys, pr=["x"]) == (2, _HEAD + "\n  ERROR: no valid PR refs provided "
                                             "via --pr: ['x']. No change made.\n\n", "")
    assert _attest(sd, capsys, pr=["x"], json=True) == (
        2, _payload("queued", "rejected_invalid_pr", path=["queued", "active", "done"]), "")
    assert _phase(sd, "ops") == "queued" and _pr_ref(sd, "ops") == ""


def _record_order(monkeypatch, calls):
    for mod, name in ((planning_cli, "_db_conn"), (tracks_lib, "_emit_track_event"),
                      (tracks_lib, "transition_phase"), (track_reconciler, "reconcile_track")):
        real = getattr(mod, name)

        def _wrap(*a, _real=real, _name=name, **k):
            calls.append({"transition_phase": f"to:{a[3]}" if len(a) > 3 else _name,
                          "_emit_track_event": f"event:{a[1]}" if len(a) > 1 else _name,
                          }.get(_name, _name))
            return _real(*a, **k)
        monkeypatch.setattr(mod, name, _wrap)


def test_attest_closes_in_order_and_renders_text(sd, capsys, monkeypatch):
    calls: list = []
    _record_order(monkeypatch, calls)
    rc, out, err = _attest(sd, capsys)
    assert (rc, err) == (0, "")
    assert out == (_HEAD + "\n  attested: why\n  pr_ref set: ops-attest:2026-11-14\n"
                   "  closed: queued -> active -> done (actor=operator, approval=APR).\n\n")
    assert calls[:2] == ["_db_conn", "event:track_ops_attest"]
    walk = [c for c in calls if c.startswith("to:") or c == "reconcile_track"]
    assert walk == ["to:active", "to:done", "reconcile_track"]
    assert calls.index("event:track_ops_attest") < calls.index("to:active")
    (event,) = _track_events(sd, "ops", "track_ops_attest")
    assert event["details"] == {"reason": "why", "approval_id": "APR",
                                "pr_ref": "ops-attest:2026-11-14", "pr_ref_before": "",
                                "pr_arg_raw": [], "pr_arg_resolved": None}
    assert _phase(sd, "ops") == "done"


def test_attest_closes_with_pr_renders_json(sd, capsys, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("refresh down")
    monkeypatch.setattr(track_reconciler, "reconcile_track", _boom)
    rc, out, err = _attest(sd, capsys, pr=["#12", "12", "13"], json=True)
    assert (rc, err) == (0, "")
    assert out == json.dumps({"track_id": "ops", "project_id": PROJECT_ID,
                              "declared_phase": "done", "action": "closed", "applied": True,
                              "path": ["queued", "active", "done"], "pr_ref": "#12,#13"},
                             indent=2, default=str) + "\n"


@pytest.mark.parametrize("exc, action, error, line", [
    (tracks_lib.TrackNotFoundError("gone"), "rejected_not_found", "gone", "  ERROR: gone\n"),
    (tracks_lib.InvalidTransitionError("bad"), "rejected_walk_failed", "bad",
     "  ERROR: transition failed at 'active': bad. Track left at 'active'.\n"),
    (ValueError("odd"), "rejected_walk_failed", "ValueError: odd",
     "  ERROR: transition failed at 'active': odd. Track left at 'active'.\n"),
])
def test_attest_walk_failures(sd, capsys, monkeypatch, exc, action, error, line):
    real = tracks_lib.transition_phase

    def _second_fails(*a, **k):
        if a[3] == "done":
            raise exc
        return real(*a, **k)
    monkeypatch.setattr(tracks_lib, "transition_phase", _second_fails)
    assert _attest(sd, capsys) == (2, _HEAD + "\n" + line + "\n", "")
    assert _attest(sd, capsys, json=True)[1] == _payload(
        "active", action, path=["active", "done"], error=error)
    assert _pr_ref(sd, "ops") == "ops-attest:2026-11-14"
