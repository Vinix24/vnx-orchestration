#!/usr/bin/env python3
"""Regression tests for the closure_verifier unknown-gate and test-run holes.

Dispatch 20260808-ds-d1-closure-gate. Two defects in closure_verifier:

1. A gate name the verifier does not implement returned PASS as soon as a
   result file existed — no status, contract_hash or report_path check.
2. Offline test-run records (kimi_gate --diff-file with a synthetic pr_id like
   0/1/2) lived in the same production results map as real gate verdicts and
   were indistinguishable from real evidence by file presence alone.

Definition of done covered here:
- A fabricated gate name with a status: pass record does NOT pass closure.
- A record marked test_run: true does NOT count as gate evidence for a known gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
from review_contract import (
    Deliverable,
    DeterministicFinding,
    QualityGate,
    ReviewContract,
    TestEvidence,
)


@pytest.fixture
def verifier_env(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_root, check=True, capture_output=True)
    (project_root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_root, check=True, capture_output=True)

    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    (dispatch_dir / "staging").mkdir(parents=True, exist_ok=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))

    feature_plan = project_root / "FEATURE_PLAN.md"
    feature_plan.write_text(
        """# Feature: Demo Feature

**Status**: Complete

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Demo PR
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Skill**: @architect
**Dependencies**: []
""",
        encoding="utf-8",
    )
    pr_queue = project_root / "PR_QUEUE.md"
    pr_queue.write_text(
        """# PR Queue - Feature: Demo Feature

## Progress Overview
Total: 1 PRs | Complete: 1 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Status

## Dependency Flow
```
PR-0 (no dependencies)
```
""",
        encoding="utf-8",
    )
    claim_file = state_dir / "closure_claim.json"
    claim_file.write_text(
        json.dumps({
            "test_files": ["FEATURE_PLAN.md"],
            "test_command": "python3 -m pytest tests/test_demo.py",
            "parallel_assignments": [{"terminal": "T1"}, {"terminal": "T2"}],
        }),
        encoding="utf-8",
    )

    return {
        "project_root": project_root,
        "feature_plan": feature_plan,
        "pr_queue": pr_queue,
        "claim_file": claim_file,
        "dispatch_dir": dispatch_dir,
    }


def _good_pr_payload(state="OPEN", merge_state="CLEAN"):
    return {
        "number": 45,
        "url": "https://example.test/pr/45",
        "state": state,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "mergeCommit": {"oid": "abc123"},
    }


def _make_contract(
    pr_id="PR-0",
    review_stack=None,
    risk_class="medium",
    content_hash="abcdef1234567890",
):
    if review_stack is None:
        review_stack = ["gemini_review"]
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Demo PR",
        feature_title="Demo Feature",
        branch="feature/demo",
        track="C",
        risk_class=risk_class,
        merge_policy="human",
        review_stack=list(review_stack),
        closure_stage="in_review",
        deliverables=[Deliverable(description="test deliverable", category="implementation")],
        non_goals=[],
        scope_files=[],
        changed_files=[],
        quality_gate=QualityGate(gate_id="gate_test", checks=["check 1"]),
        test_evidence=TestEvidence(test_files=["tests/test_demo.py"], test_command="pytest"),
        deterministic_findings=[],
        content_hash=content_hash,
    )


def _write_gate_result(results_dir, gate, pr_id, data):
    results_dir.mkdir(parents=True, exist_ok=True)
    pr_slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{pr_slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run_closure(verifier_env, contract, results_dir, monkeypatch):
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())
    return cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
        review_contract=contract,
        gate_results_dir=results_dir,
    )


# ---------------------------------------------------------------------------
# DoD 1: an unknown gate must never pass, even with a status: pass record
# ---------------------------------------------------------------------------


class TestUnknownGateNeverPasses:
    """dlv45: these tests used ``kimi_gate`` as the "unknown gate" example.
    kimi_gate is now a recognised Gate member with its own closure handler
    (see test_dlv45_gate_recognition.py), so it can no longer stand in for
    "a gate the verifier does not implement" — ``totally_unknown_gate`` takes
    over that role, preserving the original DoD-1 intent unchanged."""

    def test_unknown_gate_with_pass_record_does_not_pass_closure(self, verifier_env, monkeypatch, tmp_path):
        """Fabricated gate name + status=pass record must not yield a PASS."""
        contract = _make_contract(review_stack=["totally_unknown_gate"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "totally_unknown_gate", "PR-0", {
            "gate": "totally_unknown_gate",
            "pr_id": "PR-0",
            "status": "pass",
            "blocking_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": "/tmp/does-not-matter.md",
            "branch": "feature/demo",
        })

        result = _run_closure(verifier_env, contract, results_dir, monkeypatch)

        assert result["verdict"] == "fail"
        check = next(c for c in result["checks"] if c["name"] == "gate_totally_unknown_gate")
        assert check["status"] == "UNVERIFIED"
        assert check["status"] != "PASS"

    def test_unknown_gate_without_record_is_undecided_not_pass(self, verifier_env, monkeypatch, tmp_path):
        """Even with no result file, an unknown gate is undecided (not silently green)."""
        contract = _make_contract(review_stack=["totally_unknown_gate"])
        results_dir = tmp_path / "results"
        results_dir.mkdir(parents=True)

        result = _run_closure(verifier_env, contract, results_dir, monkeypatch)

        assert result["verdict"] == "fail"
        check = next(c for c in result["checks"] if c["name"] == "gate_totally_unknown_gate")
        assert check["status"] == "UNVERIFIED"

    def test_production_shape_unregistered_gate_record_never_passes(self, verifier_env, monkeypatch, tmp_path):
        """A real free-form-gate result (no test_run field) for an unknown gate is still refused.

        Mirrors the nine kimi_gate records that used to live in
        ~/.vnx-data/vnx-dev/state/review_gates/results/ before kimi_gate was
        promoted to a registered gate (dlv45): they carried no test_run flag
        but the gate name was unregistered, so the record was ignored, not
        treated as a passing gate. Same shape, generic unknown-gate name.
        """
        contract = _make_contract(review_stack=["totally_unknown_gate"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "totally_unknown_gate", "PR-0", {
            "gate": "totally_unknown_gate",
            "pr_id": "0",
            "pr_number": 0,
            "status": "pass",
            "reason": "verdict",
            "provider": "kimi",
            "dispatch_id": "kimi-gate-pr0-1782546770",
            "blocking_findings": [],
            "advisory_findings": [],
            "residual_risk": "",
            "recorded_at": "2026-06-27T07:56:10Z",
        })

        result = _run_closure(verifier_env, contract, results_dir, monkeypatch)

        assert result["verdict"] == "fail"
        check = next(c for c in result["checks"] if c["name"] == "gate_totally_unknown_gate")
        assert check["status"] == "UNVERIFIED"

    def test_check_single_gate_unknown_returns_unverified(self, tmp_path):
        """Direct unit check: unknown gate is UNVERIFIED regardless of record presence."""
        contract = _make_contract(review_stack=["totally_unknown_gate"])
        results_dir = tmp_path / "results"

        absent = cv._check_single_gate("totally_unknown_gate", contract, None, results_dir, "feature/demo")
        assert absent.status == "UNVERIFIED"

        present = cv._check_single_gate(
            "totally_unknown_gate", contract,
            {"gate": "totally_unknown_gate", "status": "pass"},
            results_dir, "feature/demo",
        )
        assert present.status == "UNVERIFIED"

    def test_known_gate_still_passes(self, verifier_env, monkeypatch, tmp_path):
        """Known gates keep their normal semantics — this fix must not break them."""
        report_file = tmp_path / "gemini_report.md"
        report_file.write_text("# Gemini Review\nAll clear.\n", encoding="utf-8")
        contract = _make_contract(review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
            "branch": "feature/demo",
        })

        result = _run_closure(verifier_env, contract, results_dir, monkeypatch)

        assert result["verdict"] == "pass"
        check = next(c for c in result["checks"] if c["name"] == "gate_gemini_review")
        assert check["status"] == "PASS"


# ---------------------------------------------------------------------------
# DoD 2: test-run records must not count as gate evidence
# ---------------------------------------------------------------------------


class TestTestRunRecordsRefused:
    def test_test_run_record_does_not_count_for_known_gate(self, verifier_env, monkeypatch, tmp_path):
        """A gemini_review record stamped test_run: true is refused — gate has no evidence."""
        report_file = tmp_path / "gemini_report.md"
        report_file.write_text("# Gemini Review\nAll clear.\n", encoding="utf-8")
        contract = _make_contract(review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "test_run": True,
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
            "branch": "feature/demo",
        })

        result = _run_closure(verifier_env, contract, results_dir, monkeypatch)

        assert result["verdict"] == "fail"
        check = next(c for c in result["checks"] if c["name"] == "gate_gemini_review")
        assert check["status"] == "FAIL"

    def test_find_gate_result_rejects_test_run_record(self, tmp_path):
        """_find_gate_result must return None for a test_run record even when filenames align."""
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "test_run": True,
            "contract_hash": "abcdef1234567890",
            "branch": "feature/demo",
        })

        assert cv._find_gate_result("gemini_review", "PR-0", results_dir, branch="feature/demo") is None

    def test_find_gate_result_accepts_normal_record(self, tmp_path):
        """A non-test-run record is still found normally."""
        results_dir = tmp_path / "results"
        payload = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "test_run": False,
            "contract_hash": "abcdef1234567890",
            "branch": "feature/demo",
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", payload)

        found = cv._find_gate_result("gemini_review", "PR-0", results_dir, branch="feature/demo")
        assert found is not None
        assert found["status"] == "pass"

    def test_is_test_run_record_unit(self):
        """Boolean and string forms are both recognised; absent is False."""
        assert cv._is_test_run_record({"test_run": True}) is True
        assert cv._is_test_run_record({"test_run": False}) is False
        assert cv._is_test_run_record({"test_run": "true"}) is True
        assert cv._is_test_run_record({"test_run": "1"}) is True
        assert cv._is_test_run_record({"test_run": "yes"}) is True
        assert cv._is_test_run_record({"test_run": "false"}) is False
        assert cv._is_test_run_record({}) is False
        assert cv._is_test_run_record({"test_run": None}) is False
