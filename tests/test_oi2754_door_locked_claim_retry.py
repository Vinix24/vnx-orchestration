"""OI-2754: a locked runtime DB at the door's claim is retried; a refused start does not block a refire.

Dispatch-ID: dlv-oi2754-door-locked-claim-retry

Every store is a real ``runtime_coordination.db`` in ``tmp_path`` (live shape); the door's own
functions are called directly.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import types
from pathlib import Path

import pytest

import dispatch_cli
from test_create_attempt_project_id import _build_live_shape_db

DID = "dlv-oi2754-test"


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    _build_live_shape_db(sd / "runtime_coordination.db")
    return sd


@pytest.fixture()
def sleeps(monkeypatch):
    calls: list = []
    monkeypatch.setattr(dispatch_cli, "_CLAIM_SLEEP", calls.append)
    return calls


def _spec():
    return types.SimpleNamespace(dispatch_id=DID, project_id="vnx-dev", track_id=None,
                                 gate=None, target_slot="T1")


def _lock(state_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"), isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _facts(state_dir: Path) -> list:
    path = state_dir / "dispatch_register.ndjson"
    if not path.exists():
        return []
    recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in recs if "door_bookkeeping_failed" in json.dumps(r)]


def _fast_busy(monkeypatch):
    real = dispatch_cli._rc_get_connection

    @contextlib.contextmanager
    def short(sd):
        with real(sd) as conn:
            conn.execute("PRAGMA busy_timeout = 1")
            yield conn
    monkeypatch.setattr(dispatch_cli, "_rc_get_connection", short)


def test_locked_claim_is_retried_and_claims(state_dir, sleeps, monkeypatch):
    _fast_busy(monkeypatch)
    blocker = _lock(state_dir)

    def release(delay):
        sleeps.append(delay)
        blocker.execute("ROLLBACK")
    monkeypatch.setattr(dispatch_cli, "_CLAIM_SLEEP", release)
    attempt_id = dispatch_cli._persist_dispatch_row(_spec(), state_dir=state_dir)
    assert attempt_id
    assert sleeps == [0.25]
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as c:
        assert c.execute("SELECT state FROM dispatches WHERE dispatch_id=?", (DID,)).fetchone()[0] == "claimed"
    assert _facts(state_dir) == []


def test_lock_that_never_releases_gives_one_fact_with_attempt_count(state_dir, sleeps, monkeypatch):
    _fast_busy(monkeypatch)
    blocker = _lock(state_dir)
    try:
        assert dispatch_cli._persist_dispatch_row(_spec(), state_dir=state_dir) is None
    finally:
        blocker.execute("ROLLBACK")
    assert sleeps == [0.25, 0.5, 1.0]
    facts = _facts(state_dir)
    assert len(facts) == 1
    assert "after 4 attempts" in json.dumps(facts[0])


def test_non_lock_error_is_not_retried(state_dir, sleeps, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: dispatches")
    monkeypatch.setattr(dispatch_cli, "_rc_register_dispatch", boom)
    assert dispatch_cli._persist_dispatch_row(_spec(), state_dir=state_dir) is None
    assert len(calls) == 1 and sleeps == []
    assert len(_facts(state_dir)) == 1


def _route(state_dir: Path):
    (state_dir / "route_decisions.ndjson").write_text(json.dumps({"dispatch_id": DID}, separators=(",", ":")) + "\n")


def _receipt(state_dir: Path, rec: dict):
    (state_dir / "t0_receipts.ndjson").write_text(json.dumps({"dispatch_id": DID, **rec}, separators=(",", ":")) + "\n")


REFUSAL = {"status": "blocked", "failure_reason": "start-line-refused: start_attempt_unreadable"}


def test_refused_start_with_no_attempt_clears_plain_refire(state_dir):
    _route(state_dir)
    _receipt(state_dir, REFUSAL)
    assert dispatch_cli._refire_guard_verdict(DID, state_dir=state_dir, refire_reason=None) is None


def test_route_decision_with_attempt_stays_blocked(state_dir):
    _route(state_dir)
    _receipt(state_dir, REFUSAL)
    assert dispatch_cli._persist_dispatch_row(_spec(), state_dir=state_dir)
    assert "route decision" in dispatch_cli._refire_guard_verdict(DID, state_dir=state_dir, refire_reason=None)


def test_route_decision_with_unreadable_attempts_table_stays_blocked(state_dir):
    _route(state_dir)
    _receipt(state_dir, REFUSAL)
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as c:
        c.execute("DROP TABLE dispatch_attempts")
    assert "route decision" in dispatch_cli._refire_guard_verdict(DID, state_dir=state_dir, refire_reason=None)


@pytest.mark.parametrize("rec", [None, {"status": "blocked"}, {"status": "blocked", "failure_reason": None},
                                 {"status": "failed", "failure_reason": "x"}])
def test_route_decision_without_start_refusal_stays_blocked(state_dir, rec):
    _route(state_dir)
    if rec is not None:
        _receipt(state_dir, rec)
    assert dispatch_cli._refire_guard_verdict(DID, state_dir=state_dir, refire_reason=None)
