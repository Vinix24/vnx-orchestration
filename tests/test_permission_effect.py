"""L7a2 (ADR-086): the reader over live plus archive, the effect per applied permissie proposal, the chain section."""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")]

import permission_chain as chain
import permission_effect as effect
import permission_events as pe
import permission_events_prune as prune
import permission_events_scan as scan
import permission_proposals as pp
import steering_proposal_store as store

P = "vnx-dev"
ANCHOR = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
NOW = ANCHOR + timedelta(days=8)


def ev(days, session, besluit="asked", command="git status"):
    return pe.build_event(project_id=P, session_id=session, ts=(ANCHOR + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          account="vnx", origin="t0", bron="B4a", besluit=besluit, command=command)


line = lambda event: json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"  # noqa-free: one ledger line


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def archive(state, month, events):
    path = state / "archive" / "permission_events" / f"permission_events-{month}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(map(line, events)))


def test_the_reader_returns_live_and_two_archive_months_once_in_order_and_counts_damage(state):
    sep, octo, live = ev(-62, "a"), ev(-20, "b"), ev(1, "c")
    archive(state, "2026-09", [sep])
    archive(state, "2026-10", [octo, live])  # a retried prune round: this line is live too
    (state / "permission_events.ndjson").write_text(line(live) + "not json\n" + line(ev(2, "d")) + '{"torn')
    lines, skipped, held = pe.read_window(P, "permission_events", ANCHOR - timedelta(days=70), ANCHOR + timedelta(days=3), state)
    assert [e["session_id"] for e in lines] == ["a", "b", "c", "d"]
    assert skipped == {"unparseable": 1, "torn_tail": 1} and held == ANCHOR - timedelta(days=62)


def test_the_reader_holds_a_window_start_when_an_older_month_exists_and_says_none_for_an_empty_store(state):
    assert pe.read_window(P, "permission_events", ANCHOR, NOW, state) == ([], {}, None)
    archive(state, "2026-08", [ev(-80, "old")])
    (state / "permission_events.ndjson").write_text(line(ev(1, "c")))
    lines, _skipped, held = pe.read_window(P, "permission_events", ANCHOR - timedelta(days=7), NOW, state)
    assert [e["session_id"] for e in lines] == ["c"] and held < ANCHOR - timedelta(days=7)


def test_a_prune_between_the_archive_read_and_the_live_read_waits_and_the_reader_loses_nothing(state, monkeypatch):
    archive(state, "2026-10", [ev(-20, "kept")])
    pe.write_events([ev(-1, "moved")], P, state)
    live, real_read, done = state / "permission_events.ndjson", Path.read_bytes, threading.Event()
    pruner = threading.Thread(target=lambda: (prune.prune(P, state, now=ANCHOR + timedelta(days=100)), done.set()), daemon=True)

    def paused_read(path):  # pauses the reader once, at its live read, after the archive month was read
        if path == live and threading.current_thread() is threading.main_thread() and not pruner.is_alive() and not done.is_set():
            pruner.start()
            done.wait(timeout=1)  # the old reader: prune finishes here; the fixed reader: prune waits on the lock
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", paused_read)
    lines, _skipped, _held = pe.read_window(P, "permission_events", ANCHOR - timedelta(days=30), NOW, state)
    assert [e["session_id"] for e in lines] == ["kept", "moved"]
    assert done.wait(timeout=10) and "moved" not in live.read_text()  # a raising prune never sets done


def proposal(state, evidence=True, applied_at=None):
    group = [ev(-3, s) for s in "xyz"]
    item = pp._item((group[0]["vorm"], "t0", "vnx", "project", "permissie"), group, None)
    store.reconcile(state, P, [item], now=(ANCHOR - timedelta(days=1)).isoformat())
    pid = store.proposals(state, P)[0]["proposal_id"]
    store.decide(state, P, pid, "approved", reason="ok", decided_by="vincent", now=(ANCHOR - timedelta(hours=2)).isoformat())
    extra = {"applied_evidence": {"path": "x", "rule": "Bash(git status)"}} if evidence else {}
    with store._locked(state):
        st = store._replay(store._read_records(state))
        store._commit(state, st, [store._record(P, pid, 1, "applied", ANCHOR.isoformat(), applied_ref="settings:x#0", decided_by="T0",
                                                reason="toegepast", applied_at=applied_at or ANCHOR.isoformat(), **extra)])
    return pid


def setup(state, events, cov="ok", **kw):
    pid = proposal(state, **kw)
    pe.write_events(events, P, state)
    if cov:  # None: no coverage line for the source
        pe.write_coverage(P, "B4a", state=cov, files_read=1, window="7d", state_dir=state)
    return pid


HELD = [ev(-10, "old"), ev(-1, "s1")]
COV_NOT_OK, NO_ANCHOR = {"outcome": "unknown", "reden": "coverage_not_ok"}, {"outcome": "unknown", "reden": "no_anchor"}


@pytest.mark.parametrize("events,kw,now,expected", [
    pytest.param([ev(-10, "old"), ev(-3, "s1"), ev(-2, "s1"), ev(-1, "s2"), ev(-1, "s3", besluit="executed"),
                  ev(-1, "s4", command="git log"), ev(2, "s5")], {}, NOW,
                 {"outcome": "measured", "sessions_before": 2, "sessions_after": 1}, id="distinct_sessions_before_and_after"),
    pytest.param([ev(-10, "old")], {}, ANCHOR + timedelta(days=6), {"outcome": "pending"}, id="within_seven_days_pending"),
    pytest.param([ev(-3, "s1")], {}, NOW, {"outcome": "unknown", "reden": "retention_expired"}, id="window_not_held"),
    *[pytest.param(HELD, {"cov": c}, NOW, COV_NOT_OK, id=f"coverage_{c}") for c in ("error", "absent", "unknown", None)],
    pytest.param(HELD, {"evidence": False}, NOW, NO_ANCHOR, id="no_evidence"),
    pytest.param(HELD, {"applied_at": "2026-11-01T12:00:00"}, NOW, NO_ANCHOR, id="naive_applied_at"),
])
def test_the_effect_of_an_applied_proposal(state, events, kw, now, expected):
    pid = setup(state, events, **kw)
    got = [{k: v for k, v in r.items() if k not in ("applied_at", "skipped_lines")} for r in effect.section(state, P, now)]
    assert got == [{"proposal_id": pid, **expected}]


@pytest.mark.parametrize("bad", [None, "", "x", {}, ["2026-11-01T12:00:00Z"]])
def test_anchor_refuses_missing_none_malformed_and_unknown_applied_at(bad):
    record = {"decision": "applied", "proposal_id": "p", "project_id": P, "applied_evidence": {"path": "x"}, "applied_at": bad}
    assert effect.anchor("p", P, [record]) is None
    assert effect.anchor("p", P, [{**record, "applied_at": ANCHOR.isoformat(), "project_id": "other"}]) is None
    assert effect.anchor("p", P, [{**record, "applied_at": ANCHOR.isoformat()}] * 2) is None


def test_the_chain_report_carries_the_section_and_a_raising_measurement_is_one_error_entry(state, monkeypatch):
    pid = setup(state, [ev(-10, "old"), ev(-1, "s1")])
    monkeypatch.setattr(scan, "run_scan", lambda *a, **k: 0)
    monkeypatch.setattr(pp, "build", lambda *a: ([], [], {}))
    report = chain.run_permission_chain(state, P, state.parent / "projects", NOW)
    assert report["effect"][0]["outcome"] == "measured" and report["effect"][0]["sessions_before"] == 1
    monkeypatch.setattr(effect, "measure", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    report = chain.run_permission_chain(state, P, state.parent / "projects", NOW)
    written = json.loads((state / chain.REPORT_FILE).read_text())
    assert written["effect"] == [{"proposal_id": pid, "outcome": "error", "reden": "RuntimeError"}]
    assert written["status"] == "ok" and "persist" in written
