"""The reader's door states (ADR-061 §6, track deur-afgedwongen D6).

``vnx-gate/door`` exists only after the door publishes it at merge time. One
row per branch of ``_classify_door``: published_at_merge (a), permit_revoked
(b), the re-permit of the same run (c), door_runs_ambiguous (d), a foreign or
unidentifiable App (e), any other name or binding (f), not required (g), and
every single door-App run that is not a revocation (today's reading).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import ci_contexts as cc
import forge_gate_publisher
import pr_readiness as pr

DOOR, DOOR_APP, ACTIONS = "vnx-gate/door", 4869217, 15368
REQ = cc.RequiredCheck(DOOR, DOOR_APP)
GRAPH = cc.WorkflowGraph(by_context={}, by_job={})
REPERMIT, NEW_COMMIT = "python3 scripts/pr_merge.py --pr 7", "push een nieuwe commit"


def _run(name=DOOR, conclusion="success", status="completed", run_id=77, app=DOOR_APP):
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion, "app": {"id": app}}


def _one(required, runs):
    (state,) = cc.classify_contexts([required], runs, [], GRAPH)
    return state


def test_the_door_constants_are_bound_to_the_publisher_and_the_signing_app():
    assert cc.DOOR_CONTEXT == f"{forge_gate_publisher.CHECK_RUN_NAME_PREFIX}/door" == DOOR
    doc = yaml.safe_load((REPO_ROOT / "scripts" / "forge" / "branch_protection.yaml").read_text())
    assert doc["app"]["app_id"] == cc.DOOR_APP_ID == DOOR_APP


def test_a_required_door_without_a_run_is_published_at_merge():
    state = _one(cc.RequiredCheck("vnx-gate/door", 4869217), [])
    assert state.state == "published_at_merge"
    assert (state.blocking, state.transient, state.recovery) == (False, False, None)
    assert "de deur publiceert deze bij de merge" in state.detail


_NO_APP = {"name": DOOR, "status": "completed", "conclusion": "failure"}
_CASES = [
    ("a-other-names-only", REQ, [_run("Profile A", app=ACTIONS), _run("vnx-gate/review")], "published_at_merge", None),
    ("b-door-failure", REQ, [_run(conclusion="failure")], "permit_revoked", REPERMIT),
    ("c-door-success", REQ, [_run()], cc.STATE_PASSED, None),
    ("d-failure-and-success", REQ, [_run(conclusion="failure", run_id=1), _run(run_id=2)], "door_runs_ambiguous", NEW_COMMIT),
    ("d-two-successes", REQ, [_run(run_id=1), _run(run_id=2)], "door_runs_ambiguous", NEW_COMMIT),
    ("d-three-runs", REQ, [_run(run_id=i, conclusion=c) for i, c in enumerate(["success", None, "failure"])], "door_runs_ambiguous", NEW_COMMIT),
    ("e-actions-app-success", REQ, [_run(app=ACTIONS)], cc.STATE_UNVERIFIED, None),
    ("e-actions-app-failure", REQ, [_run(app=ACTIONS, conclusion="failure")], cc.STATE_UNVERIFIED, None),
    ("e-unknown-app", REQ, [_run(app=999, conclusion="failure")], cc.STATE_UNVERIFIED, None),
    ("e-app-id-as-string", REQ, [_run(app="4869217", conclusion="failure")], cc.STATE_UNVERIFIED, None),
    ("e-app-id-none", REQ, [_run(app=None, conclusion="failure")], cc.STATE_UNVERIFIED, None),
    ("e-no-app-object", REQ, [_NO_APP], cc.STATE_UNVERIFIED, None),
    ("e-app-not-a-mapping", REQ, [dict(_NO_APP, app="4869217")], cc.STATE_UNVERIFIED, None),
    ("e-foreign-beside-door-failure", REQ, [_run(conclusion="failure", run_id=1), _run(app=ACTIONS, run_id=2)], cc.STATE_FAILED, None),
    ("e-foreign-beside-door-success", REQ, [_run(run_id=1), _run(app=ACTIONS, run_id=2)], cc.STATE_PASSED, None),
    ("today-in-progress", REQ, [_run(status="in_progress", conclusion=None)], cc.STATE_RUNNING, None),
    ("today-failure-not-completed", REQ, [_run(status="in_progress", conclusion="failure")], cc.STATE_RUNNING, None),
    ("today-status-missing", REQ, [_run(status=None, conclusion="failure")], cc.STATE_RUNNING, None),
    ("today-conclusion-missing", REQ, [_run(conclusion=None)], cc.STATE_FAILED, None),
    ("today-conclusion-empty", REQ, [_run(conclusion="")], cc.STATE_FAILED, None),
    ("today-cancelled", REQ, [_run(conclusion="cancelled")], cc.STATE_FAILED, None),
    ("today-uppercase-failure", REQ, [_run(conclusion="FAILURE")], cc.STATE_FAILED, None),
    ("f-unbound-no-run", cc.RequiredCheck(DOOR, None), [], cc.STATE_UNVERIFIED, None),
    ("f-unbound-failure", cc.RequiredCheck(DOOR, None), [_run(conclusion="failure")], cc.STATE_FAILED, None),
    ("f-other-app-binding", cc.RequiredCheck(DOOR, ACTIONS), [], cc.STATE_UNVERIFIED, None),
    ("f-plain-string", DOOR, [], cc.STATE_UNVERIFIED, None),
]


@pytest.mark.parametrize("required,runs,expected,recovery", [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_each_branch_of_the_door_reading(required, runs, expected, recovery):
    state = _one(required, runs)
    assert state.state == expected
    assert state.blocking is (expected not in (cc.STATE_PASSED, "published_at_merge"))
    assert state.recovery_for(7) == recovery
    assert state.to_dict()["recovery"] == state.recovery_for(None)


def test_the_repermit_of_the_same_run_lifts_the_block():
    revoked = _run(conclusion="failure", run_id=4242)
    assert _one(REQ, [revoked]).state == cc.STATE_PERMIT_REVOKED
    assert _one(REQ, [dict(revoked, conclusion="success")]).blocking is False


@pytest.mark.parametrize(
    "name", ["vnx-gate/dor", "vnx-gate/doors", "vnx-gate/door ", " vnx-gate/door", "VNX-GATE/DOOR", "vnx-gate/door/x", "vnx-gate/review", "door"]
)
def test_any_other_name_never_gets_the_exception(name):
    assert _one(cc.RequiredCheck(name, DOOR_APP), []).state == cc.STATE_UNVERIFIED
    state = _one(cc.RequiredCheck(name, DOOR_APP), [_run(name=name, conclusion="failure")])
    assert (state.state, state.recovery) == (cc.STATE_FAILED, None)


@pytest.mark.parametrize("door_runs", [[], [_run(conclusion="failure")], [_run(run_id=1), _run(run_id=2)]])
def test_a_door_context_that_is_not_required_changes_nothing(door_runs):
    required, profile = [cc.RequiredCheck("Profile A", ACTIONS)], [_run("Profile A", app=ACTIONS, run_id=10)]
    with_door = cc.classify_contexts(required, profile + door_runs, [], GRAPH)
    assert [s.to_dict() for s in with_door] == [s.to_dict() for s in cc.classify_contexts(required, profile, [], GRAPH)]
    assert [s.state for s in with_door] == [cc.STATE_PASSED]


@pytest.mark.parametrize(
    "door_runs,verdict,cost",
    [
        ([], pr.VERDICT_READY, None),
        ([_run(conclusion="failure")], pr.VERDICT_NOT_READY, f"{DOOR} [permit_revoked] herstel: python3 scripts/pr_merge.py --pr 4321"),
        ([_run(run_id=1, conclusion="failure"), _run(run_id=2)], pr.VERDICT_NOT_READY, f"{DOOR} [door_runs_ambiguous] herstel: {NEW_COMMIT}"),
        ([_run(app=ACTIONS)], pr.VERDICT_UNMEASURABLE, None),
    ],
    ids=["published-at-merge", "permit-revoked", "ambiguous", "foreign-app"],
)
def test_pr_readiness_shows_the_same_lines(door_runs, verdict, cost):
    states = cc.classify_contexts(
        [cc.RequiredCheck("Profile A", ACTIONS), REQ], [_run("Profile A", app=ACTIONS, run_id=10)] + door_runs, [], GRAPH
    )
    report = pr.Readiness(
        pr_number=4321, facts={"headRefOid": "c" * 40, "state": "OPEN"}, contexts=states,
        declared_gates=["codex_gate"], gates=[pr.GateEvidence(gate="codex_gate", verdict="GO", message="ok")],
    )
    assert report.verdict == verdict
    door_costs = [line for line in report.costs() if "herstel" in line]
    assert door_costs == ([cost] if cost else [])
    summary = report.to_dict()["contexts_summary"]
    assert summary["published_at_merge"] == (1 if not door_runs else 0)
    assert summary["unverified"] == (1 if verdict == pr.VERDICT_UNMEASURABLE else 0)
