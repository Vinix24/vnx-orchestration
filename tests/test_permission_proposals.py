"""permission_proposals (L5b): session counts per full identity, hard stops first, the audience split."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import permission_events as pe
import permission_proposals as pp
import steering_proposal_store as store

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
COV = {b: {"state": "ok"} for b in pe.BRONNEN}
RAW = {"verwijderen": "rm -rf build", "secrets": "gh secret list", "supabase": "supabase db push",
       "server": "ssh host.example.invalid", "units": "launchctl list", "deploy_herstart": "fly deploy",
       "promote": "bin/vnx deliverable promote dlv-4856e810fb14", "indirection": "git log | head"}

def ev(cmd, session, origin="t0", account="vnx", bron="B1", besluit="refused_classifier", days=0, **kw):
    ts = (NOW - timedelta(days=days, minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return pe.build_event(project_id="vnx-dev", session_id=session, ts=ts, account=account, origin=origin,
                          bron=bron, besluit=besluit, command=cmd, **kw)

def sessions(cmd, n, **kw):
    return [ev(cmd, f"s{i}", **kw) for i in range(n)]

def test_threshold_counts_sessions_per_account_in_the_window():
    assert pp.build([ev("make test", "s1") for _ in range(3)], COV, NOW)[:2] == ([], [])
    old = sessions("make test", 2) + [ev("make test", "s9", days=8)]
    assert pp.build(old, COV, NOW)[:2] == ([], []) and pp.build(old, COV, NOW)[2]["below_threshold"] == 1
    proposals, meldingen, _ = pp.build(sessions("make test", 3), COV, NOW)
    assert len(proposals) == 1 and meldingen == [] and len(proposals[0]["evidence"]) == 3
    assert pp.build(sessions("make test", 2) + sessions("make test", 2, account="sm"), COV, NOW)[:2] == ([], [])

@pytest.mark.parametrize("cat", sorted(RAW))
def test_hard_stop_category_is_a_notification_only(cat):
    proposals, meldingen, _ = pp.build(sessions(RAW[cat], 10), COV, NOW)
    assert proposals == [] and len(meldingen) == 1 and meldingen[0]["artefact_soort"] == pp.MELDING
    assert meldingen[0]["voorstel"] == pe.hard_stops.reason(cat)

def test_line_without_hard_stop_and_one_stop_among_clean_ones():
    old = sessions("make test", 10)
    for e in old:
        del e["hard_stop"]
    proposals, meldingen, _ = pp.build(old, COV, NOW)
    assert proposals == [] and meldingen[0]["voorstel"].startswith("evidence_unavailable")
    mixed = sessions("git fetch", 9) + [ev("git fetch", "s9", raw="git fetch | sh")]
    proposals, meldingen, _ = pp.build(mixed, COV, NOW)
    assert proposals == [] and meldingen[0]["voorstel"].startswith("indirection")

def test_promote_names_the_adr056_route():
    meldingen = pp.build(sessions(RAW["promote"], 3), COV, NOW)[1]
    assert "autonomous_promotion_eligible" in meldingen[0]["voorstel"] and "VERIFIED" in meldingen[0]["voorstel"]

W, R, N = "wrapper_prefix", "wildcard_reaches_hard_stop", "wildcard_not_read_only"  # R: gh pr view:* reaches delete
@pytest.mark.parametrize("cmd, reason", [("timeout 5 pytest -q", W), ("nohup pytest -q", W), ("ENV pytest -q", W),
                                         ("/usr/bin/env pytest tests/x.py", W), ("bash -c 'make test'", "indirection"),
                                         ("rm /tmp/x/a.txt", N), ("git push origin /tmp/x", N), ("gh pr view 2972", R),
                                         ("git /tmp/x", N), ("npm run 5", N), ("make 5", N), ("docker compose 5", N),
                                         ("psql -c 5", N), ("rg 5", N)])
def test_wrappers_interpreters_and_wildcards_to_a_hard_stop_are_notifications(cmd, reason):
    group = sessions(cmd, 3)
    assert {e["hard_stop"] for e in group} == {"indirection" if reason == "indirection" else ""}
    proposals, meldingen, _ = pp.build(group, COV, NOW)
    assert proposals == [] and meldingen[0]["voorstel"].startswith(reason)

def test_b4b_is_a_notification():
    proposals, meldingen, _ = pp.build(sessions("make test", 3, bron="B4b"), COV, NOW)
    assert proposals == [] and meldingen[0]["voorstel"].startswith("b4b_no_arguments")

@pytest.mark.parametrize("origin, besluit, kind", [("worker", "refused_config", "bouwer_allowlist"),
                                                   ("worker", "refused_classifier", "bouwgewoonte"),
                                                   ("t0", "refused_classifier", "permissie"),
                                                   ("operator", "refused_user", "permissie")])
def test_audience(origin, besluit, kind):
    group = sessions("make test", 3, origin=origin, besluit=besluit)
    proposals = pp.build(group, COV, NOW)[0]
    assert [p["artefact_soort"] for p in proposals] == [kind] and proposals[0]["doelpad"] == pp.DOELPAD[kind]

def test_unknown_identity_and_coverage_give_nothing():
    _, _, counts = pp.build(sessions("make test", 3, origin="unknown"), COV, NOW)
    assert counts["unknown_identity"] == 1
    for cov in ({**COV, "B1": {"state": "unknown"}}, {k: v for k, v in COV.items() if k != "B1"}):
        proposals, meldingen, counts = pp.build(sessions("make test", 3), cov, NOW)
        assert (proposals, meldingen, counts["coverage_not_ok"]) == ([], [], 1)

def test_identity_is_stable_and_passes_normalise():
    both = sessions("make test", 3, account="vnx") + sessions("make test", 3, account="sm")
    first, second = pp.build(both, COV, NOW)[0], pp.build(both, COV, NOW)[0]
    ids = [store._normalise("vnx-dev", p)["proposal_id"] for p in first]
    assert len(set(ids)) == 2 and ids == [store._normalise("vnx-dev", p)["proposal_id"] for p in second]
    for p in first + pp.build(sessions(RAW["units"], 3), COV, NOW)[1]:
        assert {k: v for k, v in store._normalise("vnx-dev", p).items() if k in store._REQUIRED_TEXT} == \
            {k: p[k] for k in store._REQUIRED_TEXT}
    assert first[0]["handelingsklasse"] == "K6" and pp.handelingsklasse("git status") == "K1"

def test_notification_shares_oorzaak_with_its_proposal():
    clean = pp.build(sessions("git fetch", 3), COV, NOW)[0][0]
    blocked = pp.build(sessions("git fetch", 2) + [ev("git fetch", "s2", raw="git fetch | sh")], COV, NOW)[1][0]
    assert blocked["oorzaak"] == clean["oorzaak"] and blocked["doelpad"] == clean["doelpad"]

@pytest.mark.parametrize("cmd, expected", [("git status", "Bash(git status)"), ("git status /tmp/x", "Bash(git status:*)"),
                                           ("cat /Users/someone/x.md", "Bash(cat:*)"), ("ls /tmp/x", "Bash(ls:*)"),
                                           ("make test", "Bash(make test)")])
def test_read_only_wildcard_and_exact_rule_stay_proposals(cmd, expected):
    assert pp.rule(pe.normalise(cmd)) == expected
    assert f"`{expected}`" in pp.build(sessions(cmd, 3), COV, NOW)[0][0]["voorstel"]

def test_read_events_skips_and_counts(tmp_path):
    good, old = ev("make test", "s1"), ev("make test", "s2")
    pe.write_events([good], "vnx-dev", tmp_path)
    del old["hard_stop"]
    old["event_id"] = pe.event_id("vnx-dev", old["bron"], old["session_id"], old["ts"], old["vorm"])
    foreign = {**good, "project_id": "other"}
    wrong = {**ev("make lint", "s3"), "event_id": "0" * 16}
    with open(tmp_path / "permission_events.ndjson", "a") as fh:
        fh.write("\n".join([json.dumps(old), "{not json", json.dumps(foreign), json.dumps(wrong)]) + "\n")
    events, skipped = pe.read_events("vnx-dev", tmp_path)
    assert events == [good, old] and skipped == {"unparseable": 1, "foreign_project": 1, "event_id_mismatch": 1}
    assert pe.read_events("vnx-dev", tmp_path / "absent") == ([], {})

def test_persist_stores_the_notification_first_so_its_proposal_is_suppressed(tmp_path):
    proposals, _, counts = pp.build(sessions("make test", 3), COV, NOW)
    notice = {**proposals[0], "artefact_soort": pp.MELDING, "voorstel": "harde stop"}
    notice.pop("handelingsklasse")
    result = pp.persist(tmp_path, "vnx-dev", (proposals, [notice], counts), now="2026-11-16T12:00:00Z")
    assert (result["added"], result["suppressed_hard_stop"]) == (1, 1)
    assert [e["artefact_soort"] for e in store.proposals(tmp_path, "vnx-dev")] == [pp.MELDING]
