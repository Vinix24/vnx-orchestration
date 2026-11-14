#!/usr/bin/env python3
"""Golf schoon R27 (class C): pin ``verify_pr_closure`` and ``verify_closure`` before extract-method.

Check list (name, status, detail, order), payload and side-effect order. Only the git/gh helpers are
patched; dispatches, receipts, gate results and the tracks DB are real files.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR / "lib"), str(VNX_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import closure_verifier as cv  # scripts/closure_verifier.py; no other module shares the basename
from fixtures.closure_track_fixture import CLOSURE_TRACK_ID, seed_done_track
from review_contract import (
    Deliverable,
    DeterministicFinding,
    QualityGate,
    ReviewContract,
    TestEvidence,
)

HASH = "abcdef1234567890"


def _contract(pr_id="PR-0", findings=None):
    return ReviewContract(
        pr_id=pr_id, pr_title="T", feature_title="F", branch="feature/test", track="C",
        risk_class="high", merge_policy="human", review_stack=["gemini_review", "codex_gate"],
        closure_stage="in_review", deliverables=[Deliverable(description="d", category="implementation")],
        non_goals=[], scope_files=[], changed_files=[],
        quality_gate=QualityGate(gate_id="g", checks=["c"]),
        test_evidence=TestEvidence(test_files=["tests/test_demo.py"], test_command="pytest"),
        deterministic_findings=findings or [], content_hash=HASH,
    )


def _gate_results(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    results.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    for gate, extra in (
        ("gemini_review", {"status": "pass", "blocking_count": 0, "advisory_count": 0}),
        ("codex_gate", {"verdict": "pass", "required": True, "content_hash": HASH}),
    ):
        report = reports / f"{gate}.md"
        report.write_text(f"# {gate}\nAll clear.\n", encoding="utf-8")
        (results / f"pr0-{gate}-contract.json").write_text(json.dumps({
            "branch": "feature/test", "gate": gate, "pr_id": "PR-0",
            "contract_hash": HASH, "report_path": str(report), **extra,
        }), encoding="utf-8")
    return results


def _pr(state="OPEN", merge_state="CLEAN", conclusion="SUCCESS", merge_commit=None):
    return {
        "number": 45, "state": state, "mergeStateStatus": merge_state,
        "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": conclusion}],
        "mergeCommit": merge_commit if merge_commit is not None else {"oid": "abc123"},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    state_dir = data_dir / "state"
    (dispatch_dir / "staging").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    for key, value in (
        ("VNX_HOME", VNX_ROOT), ("PROJECT_ROOT", project_root), ("VNX_DATA_DIR", data_dir),
        ("VNX_STATE_DIR", state_dir), ("VNX_DISPATCH_DIR", dispatch_dir),
        ("VNX_LOGS_DIR", data_dir / "logs"), ("VNX_PIDS_DIR", data_dir / "pids"),
        ("VNX_LOCKS_DIR", data_dir / "locks"), ("VNX_REPORTS_DIR", data_dir / "unified_reports"),
        ("VNX_DB_DIR", data_dir / "database"),
    ):
        monkeypatch.setenv(key, str(value))
    receipts = state_dir / "t0_receipts.ndjson"
    return {"project_root": project_root, "dispatch_dir": dispatch_dir, "state_dir": state_dir, "receipts": receipts}


def _completed_pr0(env):
    path = env["dispatch_dir"] / "completed" / "d0.md"
    path.parent.mkdir(parents=True)
    path.write_text("[[TARGET:B]]\nManager Block\n\nPR-ID: PR-0\nDispatch-ID: d0\n", encoding="utf-8")
    env["receipts"].write_text(json.dumps({"dispatch_id": "d0", "event_type": "task_complete", "status": "success"}) + "\n")


def _spy(monkeypatch, calls, name, result=None, passthrough=False):
    real = getattr(cv, name)

    def wrapper(*args, **kwargs):
        calls.append((name, args, kwargs))
        return real(*args, **kwargs) if passthrough else result

    monkeypatch.setattr(cv, name, wrapper)


def _rows(result):
    return [(c["name"], c["status"], c["detail"]) for c in result["checks"]]


def _per_pr(env, **kwargs):
    return cv.verify_pr_closure(
        pr_id=kwargs.pop("pr_id", "PR-0"), project_root=env["project_root"], dispatch_dir=env["dispatch_dir"],
        receipts_file=env["receipts"], state_dir=env["state_dir"], **kwargs,
    )


# --------------------------------------------------------------------------- verify_pr_closure
def test_per_pr_unknown_pr_returns_the_short_payload(env):
    assert _per_pr(env, pr_id="PR-9") == {
        "verdict": "fail", "mode": "per_pr", "pr_id": "PR-9",
        "checks": [{"name": "pr_has_dispatch", "status": "FAIL", "detail": "no dispatch names PR-9, so it is not in the queue"}],
        "reconciled_state": None, "review_evidence": None,
    }


def test_per_pr_full_pass_pins_checks_payload_and_call_order(env, tmp_path, monkeypatch):
    _completed_pr0(env)
    results = _gate_results(tmp_path)
    calls = []
    for name in ("_find_merge_record", "_validate_review_evidence", "_detect_gate_report_contradictions"):
        _spy(monkeypatch, calls, name, passthrough=True)
    _spy(monkeypatch, calls, "_find_branch_pr", result=_pr())
    findings = [DeterministicFinding(source="lint", severity="error", message="e"),
                DeterministicFinding(source="lint", severity="warning", message="w")]
    contract = _contract(findings=findings)
    result = _per_pr(env, review_contract=contract, gate_results_dir=results, branch="feature/test", require_github_pr=True)

    assert [c[0] for c in calls] == [
        "_find_merge_record", "_validate_review_evidence", "_detect_gate_report_contradictions", "_find_branch_pr",
    ]
    assert calls[0][1] == (env["receipts"], "PR-0")
    assert calls[1][1][1] == results and calls[1][2]["branch"] == "feature/test"
    names = [r[0] for r in _rows(result)]
    assert names[:3] == ["pr_has_dispatch", "pr_completed", "queue_drift"]
    assert names[-3:] == ["github_pr_exists", "github_pr_mergeable", "github_checks"]
    assert _rows(result)[2] == ("queue_drift", "PASS", "no blocking drift for PR-0")
    assert _rows(result)[-3:] == [
        ("github_pr_exists", "PASS", "GitHub PR found for branch feature/test"),
        ("github_pr_mergeable", "PASS", "PR state=OPEN mergeStateStatus=CLEAN"),
        ("github_checks", "PASS", "all required GitHub checks green"),
    ]
    assert list(result) == ["verdict", "mode", "pr_id", "branch", "reconciled_state", "checks", "review_evidence", "github_pr"]
    assert result["review_evidence"] == {
        "contract_pr_id": "PR-0", "contract_hash": HASH, "review_stack": ["gemini_review", "codex_gate"],
        "risk_class": "high", "deterministic_finding_count": 2, "error_finding_count": 1,
    }
    assert result["github_pr"] == _pr()
    assert result["reconciled_state"]["pr_id"] == "PR-0" and result["reconciled_state"]["state"] == "completed"
    expected_verdict = "pass" if all(r[1] == "PASS" for r in _rows(result)) else "fail"
    assert result["verdict"] == expected_verdict


def test_per_pr_default_results_dir_and_no_github_without_requirement(env, monkeypatch):
    _completed_pr0(env)
    calls = []
    _spy(monkeypatch, calls, "_validate_review_evidence", result=[])
    _spy(monkeypatch, calls, "_detect_gate_report_contradictions", result=[])
    _spy(monkeypatch, calls, "_find_branch_pr", result=_pr())
    result = _per_pr(env, review_contract=_contract(), branch="feature/test")
    assert calls[0][1][1] == env["state_dir"] / "review_gates" / "results"
    assert calls[1][1][1] == env["state_dir"] / "review_gates" / "results"
    assert "_find_branch_pr" not in [c[0] for c in calls]
    assert result["github_pr"] is None and result["branch"] == "feature/test"
    assert result["verdict"] == "pass"


def test_per_pr_contract_mismatch_and_red_github(env, monkeypatch):
    _completed_pr0(env)
    calls = []
    _spy(monkeypatch, calls, "_validate_review_evidence", result=[])
    _spy(monkeypatch, calls, "_find_branch_pr", result={"state": "OPEN", "mergeStateStatus": None, "statusCheckRollup": None})
    result = _per_pr(env, review_contract=_contract(pr_id="PR-7"), branch="b1", require_github_pr=True)
    assert [c[0] for c in calls] == ["_find_branch_pr"]
    assert _rows(result)[3:] == [
        ("review_contract_pr_match", "FAIL", "review contract pr_id 'PR-7' does not match closure pr_id 'PR-0'"),
        ("github_pr_exists", "PASS", "GitHub PR found for branch b1"),
        ("github_pr_mergeable", "FAIL",
         "PR state=OPEN mergeStateStatus=unknown — PR must be OPEN and CLEAN for merge readiness"),
        ("github_checks", "FAIL", "GitHub checks incomplete or failing — merge readiness blocked"),
    ]
    assert result["review_evidence"] is None and result["verdict"] == "fail"


def test_per_pr_no_contract_and_missing_github_pr(env, monkeypatch):
    _completed_pr0(env)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: None)
    result = _per_pr(env, branch="b2", require_github_pr=True)
    assert _rows(result)[3:] == [
        ("review_contract", "FAIL", "no review contract provided — per-PR closure requires contract-backed evidence"),
        ("github_pr_exists", "FAIL", "GitHub PR missing for branch b2"),
    ]
    assert result["github_pr"] is None and result["verdict"] == "fail"


# --------------------------------------------------------------------------- verify_closure
@pytest.fixture
def track_env(env, monkeypatch):
    seed_done_track(env["state_dir"], monkeypatch)
    claim = env["state_dir"] / "closure_claim.json"
    claim.write_text(json.dumps({"test_files": [], "test_command": "", "parallel_assignments": []}), encoding="utf-8")
    env["claim"] = claim
    return env


def _closure(env, mode, **kwargs):
    return cv.verify_closure(project_root=env["project_root"], track_id=CLOSURE_TRACK_ID, branch="feature/demo",
                             mode=mode, claim_file=env["claim"], **kwargs)


def _spy_feature(monkeypatch, calls, pr, on_main=True):
    _spy(monkeypatch, calls, "_remote_branch_exists", result=True)
    _spy(monkeypatch, calls, "_find_branch_pr", result=pr)
    _spy(monkeypatch, calls, "_merge_commit_on_main", result=on_main)
    for name in ("_validate_test_claims", "_check_stale_staging", "_feature_merge_record"):
        _spy(monkeypatch, calls, name, passthrough=True)
    _spy(monkeypatch, calls, "_validate_review_evidence", result=[])
    _spy(monkeypatch, calls, "_detect_gate_report_contradictions", result=[])


def test_closure_pre_merge_with_contract_pins_order_and_payload(track_env, monkeypatch):
    calls = []
    _spy_feature(monkeypatch, calls, _pr(merge_state="DIRTY"))
    result = _closure(track_env, "pre_merge", review_contract=_contract())
    assert [c[0] for c in calls] == [
        "_remote_branch_exists", "_find_branch_pr", "_validate_test_claims", "_check_stale_staging",
        "_feature_merge_record", "_validate_review_evidence", "_detect_gate_report_contradictions",
    ]
    default_dir = track_env["state_dir"] / "review_gates" / "results"
    assert calls[5][1][1] == default_dir and calls[6][1][1] == default_dir
    assert calls[5][2] == {"branch": "feature/demo", "merge_record": {"event_type": "pr_merged", "pr_number": 45}}
    rows = _rows(result)
    assert [r[0] for r in rows[:2]] == ["track_derived_status", "track_prs_delivered"]
    assert rows[2:6] == [
        ("branch_pushed", "PASS", "remote branch found: feature/demo"),
        ("pr_exists", "PASS", "PR found for branch feature/demo"),
        ("merge_state", "FAIL", "PR state=OPEN mergeStateStatus=DIRTY"),
        ("github_checks", "PASS", "all required GitHub checks green"),
    ]
    assert rows[-1][0] == "stale_staging"
    assert list(result) == ["verdict", "mode", "branch", "track_id", "feature_title", "checks", "pr",
                            "claim_file", "review_evidence"]
    assert result["claim_file"] == str(track_env["claim"]) and result["track_id"] == CLOSURE_TRACK_ID
    assert result["review_evidence"]["deterministic_finding_count"] == 0
    assert result["verdict"] == "fail"


def test_closure_post_merge_merged_on_main(track_env, monkeypatch):
    calls = []
    _spy_feature(monkeypatch, calls, _pr(state="MERGED"))
    result = _closure(track_env, "post_merge")
    assert calls[2][0] == "_merge_commit_on_main" and calls[2][1] == ("abc123", track_env["project_root"])
    rows = _rows(result)
    assert rows[4:6] == [
        ("pr_merged", "PASS", "PR state=MERGED"),
        ("merge_commit_on_main", "PASS", "merge commit present on origin/main"),
    ]
    assert rows[-1] == ("review_contract", "FAIL", "no review contract provided — closure requires contract-backed evidence")
    assert result["review_evidence"] is None


def test_closure_post_merge_without_merge_commit_skips_git(track_env, monkeypatch):
    calls = []
    _spy_feature(monkeypatch, calls, _pr(state="OPEN", merge_commit="not-a-dict"))
    result = _closure(track_env, "post_merge")
    assert "_merge_commit_on_main" not in [c[0] for c in calls]
    assert _rows(result)[4:6] == [
        ("pr_merged", "FAIL", "PR state=OPEN"),
        ("merge_commit_on_main", "FAIL", "merge commit missing from origin/main"),
    ]


def test_closure_without_pr_has_no_merge_checks(track_env, monkeypatch):
    calls = []
    _spy_feature(monkeypatch, calls, None)
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, root: False)
    result = cv.verify_closure(
        project_root=track_env["project_root"], track_id=CLOSURE_TRACK_ID, branch="feature/demo", mode="pre_merge")
    rows = _rows(result)
    assert rows[2:4] == [
        ("branch_pushed", "FAIL", "remote branch missing: feature/demo"),
        ("pr_exists", "FAIL", "PR missing for branch feature/demo"),
    ]
    assert rows[4][0] not in {"merge_state", "github_checks", "pr_merged"}
    assert result["pr"] is None and result["claim_file"] is None and result["verdict"] == "fail"
