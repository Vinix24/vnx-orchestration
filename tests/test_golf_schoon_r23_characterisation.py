#!/usr/bin/env python3
"""Characterisation of the three audit-view functions (golf schoon R23)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from provenance_verification import (
    ADVISORY_RESOLVE_ESCALATION,
    ADVISORY_REVIEW_OVERRIDE,
    Advisory,
    VerificationFinding,
    VerificationResult,
)
from runtime_coordination import get_connection, init_schema

import provenance_audit_views as pav  # after provenance_verification: circular import order


@pytest.fixture
def conn(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    with get_connection(sd) as c:
        yield c


def _event(c, eid, etype, entity, at, meta):
    c.execute(
        "INSERT INTO coordination_events (event_id,event_type,entity_type,entity_id,"
        "actor,reason,metadata_json,occurred_at) VALUES (?,?,?,?,?,?,?,?)",
        (eid, etype, "dispatch", entity, "t", "why-" + eid, meta, at),
    )


def _esc(c, entity, level, at, resolved=None, etype="dispatch"):
    c.execute(
        "INSERT INTO escalation_state (entity_type,entity_id,escalation_level,"
        "trigger_description,policy_class,updated_at,resolved_at) VALUES (?,?,?,?,?,?,?)",
        (etype, entity, level, "trig-" + entity, "pc", at, resolved),
    )


def _ovr(c, oid, entity, outcome, at):
    c.execute(
        "INSERT INTO governance_overrides (override_id,entity_type,entity_id,actor,"
        "override_type,justification,outcome,previous_level,new_level,occurred_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, "dispatch", entity, "op", "skip", "just-" + oid, outcome, "hold", "info", at),
    )


@pytest.fixture
def gov(conn):
    _event(conn, "e1", "policy_evaluation", "d1", "2026-01-01T00:00:01Z",
           json.dumps({"outcome": "gated", "action": "merge", "policy_class": "p",
                       "escalation_level": "hold", "enforcement": "block"}))
    _event(conn, "e2", "policy_evaluation", "d2", "2026-01-01T00:00:02Z",
           json.dumps({"outcome": "weird"}))
    _event(conn, "e3", "policy_evaluation", "d1", "2026-01-01T00:00:03Z", "{}")
    _event(conn, "e4", "other", "d1", "2026-01-01T00:00:04Z", "{}")
    _esc(conn, "d1", "hold", "2026-01-02T00:00:00Z")
    _esc(conn, "d2", "escalate", "2026-01-01T00:00:00Z")
    _esc(conn, "d3", "info", "2026-01-03T00:00:00Z")
    _esc(conn, "d4", "hold", "2026-01-04T00:00:00Z", resolved="2026-01-05T00:00:00Z")
    _ovr(conn, "o1", "d1", "granted", "2026-01-01T00:00:00Z")
    _ovr(conn, "o2", "d2", "denied", "2026-01-02T00:00:00Z")
    _ovr(conn, "o3", "d1", "other", "2026-01-03T00:00:00Z")
    conn.commit()
    return conn


def test_governance_system_wide(gov):
    v = pav.governance_audit_view(gov)
    assert v["dispatch_id"] is None
    assert [e["event_id"] for e in v["policy_evaluations"]] == ["e3", "e2", "e1"]
    assert v["policy_evaluations"][2] == {
        "event_id": "e1", "entity": "dispatch:d1", "action": "merge", "outcome": "gated",
        "policy_class": "p", "escalation_level": "hold", "enforcement": "block",
        "occurred_at": "2026-01-01T00:00:01Z",
    }
    assert v["policy_evaluations"][0]["outcome"] == "unknown"
    # severity ordering first, then recency
    assert [(e["entity"], e["level"]) for e in v["escalations"]] == [
        ("dispatch:d2", "escalate"), ("dispatch:d1", "hold"), ("dispatch:d3", "info")]
    assert v["escalations"][0] == {
        "entity": "dispatch:d2", "level": "escalate", "trigger": "trig-d2",
        "policy_class": "pc", "since": "2026-01-01T00:00:00Z"}
    assert [o["override_id"] for o in v["overrides"]] == ["o3", "o2", "o1"]
    assert v["overrides"][2] == {
        "override_id": "o1", "entity": "dispatch:d1", "actor": "op", "type": "skip",
        "outcome": "granted", "justification": "just-o1", "previous_level": "hold",
        "new_level": "info", "occurred_at": "2026-01-01T00:00:00Z"}
    assert v["summary"] == {
        "evaluation_count": 3,
        "outcome_counts": {"automatic": 0, "gated": 1, "forbidden": 0,
                           "unknown": 1, "weird": 1},
        "escalation_counts": {"info": 1, "review_required": 0, "hold": 1, "escalate": 1},
        "blocking_count": 2,
        "override_counts": {"granted": 1, "denied": 1, "other": 1},
    }


def test_governance_dispatch_filter_and_limit(gov):
    v = pav.governance_audit_view(gov, dispatch_id="d1")
    assert v["dispatch_id"] == "d1"
    assert [e["event_id"] for e in v["policy_evaluations"]] == ["e3", "e1"]
    assert [e["entity"] for e in v["escalations"]] == ["dispatch:d1"]
    assert [o["override_id"] for o in v["overrides"]] == ["o3", "o1"]



@pytest.fixture
def prov(conn):
    conn.execute(
        "INSERT INTO provenance_registry (dispatch_id,receipt_id,commit_sha,pr_number,"
        "chain_status,gaps_json,registered_at,verified_at) VALUES "
        "('p1','r1','abc',7,'receipt_and_commit','[\"g\"]','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z'),"
        "('p2',NULL,NULL,NULL,'odd','bad','2026-01-03T00:00:00Z',NULL)")
    conn.execute(
        "INSERT INTO provenance_verifications (verification_id,dispatch_id,verdict,"
        "chain_status,findings_json,advisory_json,verified_by,verified_at) VALUES "
        "('v1','p1','pass','receipt_and_commit','[1]','[2]','me','2026-01-01T00:00:00Z'),"
        "('v2','p2','fail','broken',NULL,'x','you','2026-01-02T00:00:00Z'),"
        "('v3','p1','zzz','incomplete','[]','[]','me','2026-01-03T00:00:00Z')")
    _event(conn, "g1", "provenance_gap", "p1", "2026-01-01T00:00:00Z",
           json.dumps({"gap_type": "commit", "severity": "high"}))
    _event(conn, "g2", "provenance_gap", "p2", "2026-01-02T00:00:00Z", "{}")
    _event(conn, "g3", "x", "p2", "2026-01-03T00:00:00Z", "{}")
    conn.commit()
    return conn


def test_provenance_system_wide(prov):
    v = pav.provenance_audit_view(prov)
    assert v["dispatch_id"] is None
    assert [r["dispatch_id"] for r in v["registry_entries"]] == ["p2", "p1"]
    assert v["registry_entries"][1] == {
        "dispatch_id": "p1", "receipt_id": "r1", "commit_sha": "abc", "pr_number": 7,
        "chain_status": "receipt_and_commit", "gaps": ["g"],
        "verified_at": "2026-01-02T00:00:00Z", "registered_at": "2026-01-01T00:00:00Z"}
    assert [x["verification_id"] for x in v["verifications"]] == ["v3", "v2", "v1"]
    assert v["verifications"][2] == {
        "verification_id": "v1", "dispatch_id": "p1", "verdict": "pass",
        "chain_status": "receipt_and_commit", "findings": [1], "advisories": [2],
        "verified_by": "me", "verified_at": "2026-01-01T00:00:00Z"}
    assert v["verifications"][1]["findings"] == v["verifications"][1]["advisories"] == []
    assert [g["event_id"] for g in v["gap_events"]] == ["g2", "g1"]
    assert v["gap_events"][1] == {
        "event_id": "g1", "entity": "dispatch:p1", "gap_type": "commit",
        "severity": "high", "reason": "why-g1", "occurred_at": "2026-01-01T00:00:00Z"}
    assert v["summary"] == {
        "registry_count": 2,
        "chain_status_counts": {"receipt_and_commit": 1, "incomplete": 0, "broken": 0, "odd": 1},
        "verification_count": 3,
        "verdict_counts": {"pass": 1, "warning": 0, "fail": 1, "zzz": 1},
        "gap_event_count": 2,
    }


def test_provenance_filter_and_limit(prov):
    v = pav.provenance_audit_view(prov, dispatch_id="p1", limit=1)
    assert [r["dispatch_id"] for r in v["registry_entries"]] == ["p1"]
    assert [x["verification_id"] for x in v["verifications"]] == ["v3"]
    assert [g["event_id"] for g in v["gap_events"]] == ["g1"]



def _fake_verify(calls):
    def fake(conn, dispatch_id, receipts_path, repo_root=None):
        calls.append((dispatch_id, receipts_path, repo_root))
        findings = [
            VerificationFinding(finding_type="a", severity="error", entity_type="dispatch",
                                entity_id=dispatch_id, description="bad", layer="l"),
            VerificationFinding(finding_type="b", severity="warning", entity_type="dispatch",
                                entity_id=dispatch_id, description="meh", layer="l"),
            VerificationFinding(finding_type="c", severity="info", entity_type="dispatch",
                                entity_id=dispatch_id, description="fyi", layer="l"),
        ]
        adv = [Advisory(advisory_type="t" + dispatch_id, severity="info", entity_type="dispatch",
                        entity_id=dispatch_id, recommendation="rec", evidence={})]
        return VerificationResult(dispatch_id=dispatch_id, verdict="fail",
                                  chain_status="broken", findings=findings, advisories=adv)
    return fake


def test_pre_merge_advisory_full(gov, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pav, "verify_dispatch_provenance", _fake_verify(calls))
    _esc(gov, "d5", "escalate", "2026-01-06T00:00:00Z")
    _ovr(gov, "o5", "d5", "granted", "2026-01-06T00:00:00Z")
    gov.commit()
    rp = tmp_path / "r.ndjson"
    out = pav.pre_merge_advisory(gov, ["d1", "d5", "dX"], rp, tmp_path)
    assert calls == [("d1", rp, tmp_path), ("d5", rp, tmp_path), ("dX", rp, tmp_path)]
    assert out["ready"] is False
    types = [(b["dispatch_id"], b["finding_type"]) for b in out["blockers"]]
    assert types[:3] == [("d1", "a"), ("d5", "a"), ("dX", "a")]
    assert sorted(types[3:]) == [("d1", "escalation_hold"), ("d5", "escalation_escalate")]
    hold = [b for b in out["blockers"] if b["finding_type"] == "escalation_hold"][0]
    assert hold == {"finding_type": "escalation_hold", "severity": "error",
                    "entity_type": "dispatch", "entity_id": "d1", "dispatch_id": "d1",
                    "description": "Dispatch on hold: trig-d1", "layer": "governance"}
    esc = [b for b in out["blockers"] if b["finding_type"] == "escalation_escalate"][0]
    assert esc["description"] == "Dispatch escalated to T0: trig-d5"
    assert [(w["dispatch_id"], w["finding_type"]) for w in out["warnings"]] == [
        ("d1", "b"), ("d5", "b"), ("dX", "b")]
    advs = out["advisories"]
    assert [a["advisory_type"] for a in advs] == [
        "td1", "td5", "tdX", ADVISORY_RESOLVE_ESCALATION, ADVISORY_REVIEW_OVERRIDE]
    assert advs[3]["severity"] == "warning"
    assert advs[3]["recommendation"] == "1 dispatch(es) on hold — resolve before merge"
    assert advs[3]["evidence"]["holds"][0] == {
        "entity": "dispatch:d1", "level": "hold", "trigger": "trig-d1"}
    assert advs[4]["severity"] == "info"
    assert advs[4]["recommendation"] == (
        "2 governance override(s) granted for dispatches in scope — "
        "review justifications before merge")
    assert [o["override_id"] for o in advs[4]["evidence"]["overrides"]] == ["o5", "o1"]
    assert out["governance"]["override_count"] == 2
    assert [h["entity"] for h in out["governance"]["holds"]] == ["dispatch:d1"]
    assert [h["entity"] for h in out["governance"]["escalations"]] == ["dispatch:d5"]


def test_pre_merge_advisory_clean(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        pav, "verify_dispatch_provenance",
        lambda c, d, r, root=None: VerificationResult(
            dispatch_id=d, verdict="pass", chain_status="complete", findings=[], advisories=[]))
    _esc(conn, "other", "hold", "2026-01-01T00:00:00Z")
    _esc(conn, "dq", "info", "2026-01-01T00:00:00Z")
    _ovr(conn, "od", "dq", "denied", "2026-01-01T00:00:00Z")
    conn.commit()
    out = pav.pre_merge_advisory(conn, ["dq"], tmp_path / "r")
    assert out == {"ready": True, "blockers": [], "warnings": [], "advisories": [],
                   "governance": {"holds": [], "escalations": [], "override_count": 0}}




def test_governance_malformed_metadata_json_raises(conn):
    # pinned as-is: _parse_json gives [] and the view calls .get on it
    _event(conn, "bad", "policy_evaluation", "d1", "2026-01-01T00:00:00Z", "not json")
    conn.commit()
    with pytest.raises(AttributeError):
        pav.governance_audit_view(conn)
