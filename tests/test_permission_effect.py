"""L7a2 (ADR-086): the reader over live plus archive, the effect per applied permissie proposal, the chain section."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")]

import permission_chain as chain
import permission_effect as effect
import permission_events as pe
import permission_events_scan as scan
import permission_proposals as pp
import steering_proposal_store as store

P = "vnx-dev"
ANCHOR = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
NOW = ANCHOR + timedelta(days=8)


def ev(days, session, besluit="asked", command="git status"):
    return pe.build_event(project_id=P, session_id=session, ts=(ANCHOR + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          account="vnx", origin="t0", bron="B4a", besluit=besluit, command=command)


def line(event):
    return json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"


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
    pe.write_coverage(P, "B4a", state=cov, files_read=1, window="7d", state_dir=state)
    return pid


def outcome(state, now=NOW):
    return [{k: v for k, v in r.items() if k not in ("applied_at", "skipped_lines")} for r in effect.section(state, P, now)]


def test_both_numbers_count_distinct_sessions_of_the_form_before_and_after_the_anchor(state):
    events = [ev(-10, "old"), ev(-3, "s1"), ev(-2, "s1"), ev(-1, "s2"), ev(-1, "s3", besluit="executed"),
              ev(-1, "s4", command="git log"), ev(2, "s5")]
    pid = setup(state, events)
    assert outcome(state) == [{"proposal_id": pid, "outcome": "measured", "sessions_before": 2, "sessions_after": 1}]


def test_within_seven_days_of_applied_at_is_pending(state):
    pid = setup(state, [ev(-10, "old")])
    assert outcome(state, ANCHOR + timedelta(days=6)) == [{"proposal_id": pid, "outcome": "pending"}]


def test_a_window_before_what_the_store_holds_is_retention_expired(state):
    pid = setup(state, [ev(-3, "s1")])
    assert outcome(state) == [{"proposal_id": pid, "outcome": "unknown", "reden": "retention_expired"}]


@pytest.mark.parametrize("cov", ["error", "absent", "unknown"])
def test_a_source_of_the_form_not_ok_is_coverage_not_ok(state, cov):
    pid = setup(state, [ev(-10, "old"), ev(-1, "s1")], cov=cov)
    assert outcome(state) == [{"proposal_id": pid, "outcome": "unknown", "reden": "coverage_not_ok"}]


def test_no_coverage_line_for_the_source_is_coverage_not_ok(state):
    pid = proposal(state)
    pe.write_events([ev(-10, "old"), ev(-1, "s1")], P, state)
    assert outcome(state) == [{"proposal_id": pid, "outcome": "unknown", "reden": "coverage_not_ok"}]


@pytest.mark.parametrize("kw", [{"evidence": False}, {"applied_at": "2026-11-01T12:00:00"}])
def test_an_applied_record_without_evidence_or_an_aware_applied_at_is_no_anchor(state, kw):
    pid = setup(state, [ev(-10, "old"), ev(-1, "s1")], **kw)
    assert outcome(state) == [{"proposal_id": pid, "outcome": "unknown", "reden": "no_anchor"}]


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
