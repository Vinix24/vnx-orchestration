#!/usr/bin/env python3

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
        json.dumps(
            {
                "test_files": ["FEATURE_PLAN.md"],
                "test_command": "python3 -m pytest tests/test_demo.py",
                "parallel_assignments": [{"terminal": "T1"}, {"terminal": "T2"}],
            }
        ),
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


def test_verify_closure_fails_when_pr_missing(verifier_env, monkeypatch):
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: None)

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    assert result["verdict"] == "fail"
    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "pr_exists" in failed


def test_verify_closure_fails_on_metadata_drift(verifier_env, monkeypatch):
    verifier_env["pr_queue"].write_text(
        """# PR Queue - Feature: Wrong Feature

## Progress Overview
Total: 1 PRs | Complete: 1 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Status

## Dependency Flow
```
PR-0 -> PR-1
```
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload())

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "metadata_sync" in failed


def test_verify_closure_fails_when_stale_staging_dispatches_present(verifier_env, monkeypatch):
    stale_dispatch = verifier_env["dispatch_dir"] / "staging" / "stale.md"
    stale_dispatch.write_text("PR-ID: PR-999\n", encoding="utf-8")
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload())

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="pre_merge",
        claim_file=verifier_env["claim_file"],
    )

    failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
    assert "stale_staging" in failed


def test_verify_closure_passes_for_valid_post_merge_state(verifier_env, monkeypatch):
    monkeypatch.setattr(cv, "_remote_branch_exists", lambda branch, project_root: True)
    monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: _good_pr_payload(state="MERGED"))
    monkeypatch.setattr(cv, "_merge_commit_on_main", lambda oid, project_root: True)

    result = cv.verify_closure(
        project_root=verifier_env["project_root"],
        feature_plan=verifier_env["feature_plan"],
        pr_queue=verifier_env["pr_queue"],
        branch="feature/demo",
        mode="post_merge",
        claim_file=verifier_env["claim_file"],
    )

    # Without review contract, verdict is fail (contract required)
    assert result["verdict"] == "fail"
    failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
    assert "review_contract" in failed


# ---------------------------------------------------------------------------
# Helpers for review contract enforcement tests
# ---------------------------------------------------------------------------

_DEFAULT_REVIEW_STACK = ["gemini_review", "codex_gate", "claude_github_optional"]


def _make_contract(
    pr_id="PR-0",
    review_stack=_DEFAULT_REVIEW_STACK,
    risk_class="medium",
    changed_files=None,
    deterministic_findings=None,
    content_hash="abcdef1234567890",
):
    """Build a minimal ReviewContract for testing."""
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
        changed_files=changed_files or [],
        quality_gate=QualityGate(gate_id="gate_test", checks=["check 1"]),
        test_evidence=TestEvidence(test_files=["tests/test_demo.py"], test_command="pytest"),
        deterministic_findings=deterministic_findings or [],
        content_hash=content_hash,
    )


def _write_gate_result(results_dir, gate, pr_id, data):
    """Write a gate result JSON file to the results directory."""
    results_dir.mkdir(parents=True, exist_ok=True)
    pr_slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{pr_slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_gemini_result(pr_id="PR-0", status="pass", blocking_count=0, advisory_count=0, contract_hash="abcdef1234567890", report_path=""):
    return {
        "gate": "gemini_review",
        "pr_id": pr_id,
        "status": status,
        "blocking_count": blocking_count,
        "advisory_count": advisory_count,
        "contract_hash": contract_hash,
        "report_path": report_path,
    }


def _make_codex_result(pr_id="PR-0", verdict="pass", contract_hash="abcdef1234567890", report_path=""):
    return {
        "gate": "codex_final_gate",
        "pr_id": pr_id,
        "verdict": verdict,
        "required": True,
        "content_hash": contract_hash,
        "contract_hash": contract_hash,
        "report_path": report_path,
    }


def _make_claude_result(pr_id="PR-0", state="not_configured", contract_hash="abcdef1234567890"):
    return {
        "gate": "claude_github_optional",
        "pr_id": pr_id,
        "state": state,
        "contributed_evidence": state in ("requested", "completed"),
        "was_intentionally_absent": state in ("not_configured", "configured_dry_run"),
        "contract_hash": contract_hash,
    }


# ---------------------------------------------------------------------------
# Review contract enforcement tests
# ---------------------------------------------------------------------------


class TestClosureVerifierContractEnforcement:
    """Tests for gate_pr5_closure_contract_enforcement quality gate."""

    def test_fails_when_no_review_contract_provided(self, verifier_env, monkeypatch):
        """Closure verifier fails when no review contract is given."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=None,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "review_contract" in failed
        assert result["review_evidence"] is None

    def test_fails_when_contract_has_empty_review_stack(self, verifier_env, monkeypatch, tmp_path):
        """Contract with empty review_stack is rejected."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=[])
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "review_contract" in failed

    def test_fails_when_gemini_result_missing(self, verifier_env, monkeypatch, tmp_path):
        """Missing Gemini gate result blocks closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_gemini_review" in failed

    def test_fails_when_gemini_has_blocking_findings(self, verifier_env, monkeypatch, tmp_path):
        """Gemini review with blocking findings blocks closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _make_gemini_result(status="fail", blocking_count=2))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_gemini_review" in failed

    def test_fails_when_codex_gate_required_but_missing(self, verifier_env, monkeypatch, tmp_path):
        """High-risk PR with codex_gate in stack fails when no result present."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["codex_gate"],
            risk_class="high",
        )
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_codex_gate" in failed
        # Verify the detail mentions "required"
        codex_check = next(c for c in result["checks"] if c["name"] == "gate_codex_gate")
        assert "required" in codex_check["detail"]

    def test_fails_when_codex_gate_verdict_not_pass(self, verifier_env, monkeypatch, tmp_path):
        """Codex gate with non-pass verdict blocks closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["codex_gate"],
            risk_class="high",
        )
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _make_codex_result(verdict="fail"))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_codex_gate" in failed

    def test_codex_gate_passes_when_not_required_by_policy(self, verifier_env, monkeypatch, tmp_path):
        """Low-risk PR does not require codex gate even when in review stack."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["codex_gate"],
            risk_class="low",
            changed_files=["README.md"],
        )
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        codex_check = next(c for c in result["checks"] if c["name"] == "gate_codex_gate")
        assert codex_check["status"] == "PASS"
        assert "not required" in codex_check["detail"]

    def test_fails_when_claude_github_result_missing(self, verifier_env, monkeypatch, tmp_path):
        """Optional Claude GitHub gate still needs explicit state."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["claude_github_optional"])
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_claude_github_optional" in failed

    def test_claude_github_passes_when_intentionally_absent(self, verifier_env, monkeypatch, tmp_path):
        """Claude GitHub gate passes when explicitly not_configured."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["claude_github_optional"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "claude_github_optional", "PR-0",
                           _make_claude_result(state="not_configured"))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        claude_check = next(c for c in result["checks"] if c["name"] == "gate_claude_github_optional")
        assert claude_check["status"] == "PASS"

    def test_fails_when_content_hash_mismatch(self, verifier_env, monkeypatch, tmp_path):
        """Stale evidence (hash mismatch) blocks closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["gemini_review"],
            content_hash="aaaa111122223333",
        )
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _make_gemini_result(contract_hash="bbbb444455556666"))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "hash_gemini_review" in failed

    def test_fails_when_deterministic_findings_have_errors(self, verifier_env, monkeypatch, tmp_path):
        """Unresolved error-severity deterministic findings block closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["gemini_review"],
            deterministic_findings=[
                DeterministicFinding(source="lint", severity="error", message="syntax error"),
                DeterministicFinding(source="lint", severity="warning", message="unused var"),
            ],
        )
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", _make_gemini_result())

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "deterministic_findings" in failed

    def test_passes_with_full_evidence_stack(self, verifier_env, monkeypatch, tmp_path):
        """Full review stack with all passing evidence clears closure."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        # Create report files that report_path will reference.
        reports_dir = tmp_path / "headless_reports"
        reports_dir.mkdir(parents=True)
        gemini_report = reports_dir / "gemini-PR-0.md"
        gemini_report.write_text("# Gemini Review\n")
        codex_report = reports_dir / "codex-PR-0.md"
        codex_report.write_text("# Codex Gate\n")

        contract = _make_contract(
            review_stack=["gemini_review", "codex_gate", "claude_github_optional"],
            risk_class="high",
            deterministic_findings=[
                DeterministicFinding(source="lint", severity="warning", message="minor style"),
            ],
        )
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _make_gemini_result(report_path=str(gemini_report)))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _make_codex_result(report_path=str(codex_report)))
        _write_gate_result(results_dir, "claude_github_optional", "PR-0",
                           _make_claude_result(state="not_configured"))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "pass"
        assert result["review_evidence"] is not None
        assert result["review_evidence"]["contract_pr_id"] == "PR-0"
        assert result["review_evidence"]["review_stack"] == ["gemini_review", "codex_gate", "claude_github_optional"]
        assert result["review_evidence"]["error_finding_count"] == 0

    def test_review_evidence_summary_visible_in_output(self, verifier_env, monkeypatch, tmp_path):
        """Review evidence summary is included in closure output."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", _make_gemini_result())

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        evidence = result["review_evidence"]
        assert evidence is not None
        assert evidence["contract_hash"] == "abcdef1234567890"
        assert evidence["risk_class"] == "medium"
        assert evidence["deterministic_finding_count"] == 0

    def test_false_green_blocked_missing_required_gate(self, verifier_env, monkeypatch, tmp_path):
        """False-green scenario: all other checks pass but required gate result missing."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        # Provide gemini and claude but omit required codex for high-risk
        contract = _make_contract(
            review_stack=["gemini_review", "codex_gate", "claude_github_optional"],
            risk_class="high",
        )
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", _make_gemini_result())
        _write_gate_result(results_dir, "claude_github_optional", "PR-0",
                           _make_claude_result(state="not_configured"))
        # Deliberately omit codex_gate result

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_codex_gate" in failed
        # Gemini and Claude should still pass
        passed = {c["name"] for c in result["checks"] if c["status"] == "PASS"}
        assert "gate_gemini_review" in passed
        assert "gate_claude_github_optional" in passed

    def test_false_green_blocked_stale_evidence(self, verifier_env, monkeypatch, tmp_path):
        """False-green scenario: gate passed but evidence is stale (hash mismatch)."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(
            review_stack=["gemini_review"],
            content_hash="current_hash_1234",
        )
        results_dir = tmp_path / "results"
        # Gate result has old hash
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _make_gemini_result(contract_hash="old_stale_hash_999"))

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "hash_gemini_review" in failed

    def test_claude_github_ambiguous_state_fails(self, verifier_env, monkeypatch, tmp_path):
        """Claude GitHub gate with ambiguous state (no explicit absent/evidence) fails."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        contract = _make_contract(review_stack=["claude_github_optional"])
        results_dir = tmp_path / "results"
        # Write a result that has neither contributed_evidence nor was_intentionally_absent
        _write_gate_result(results_dir, "claude_github_optional", "PR-0", {
            "gate": "claude_github_optional",
            "pr_id": "PR-0",
            "state": "blocked",
            "contributed_evidence": False,
            "was_intentionally_absent": False,
            "contract_hash": "abcdef1234567890",
        })

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_claude_github_optional" in failed


# ---------------------------------------------------------------------------
# Round-1 codex finding regressions (PR #322 / CFX-3)
# ---------------------------------------------------------------------------


def _make_gate_artifact_payload(
    *,
    gate,
    pr_id="PR-0",
    contract_hash="abcdef1234567890",
    report_path="",
    blocking=None,
    advisory=None,
):
    """Build a gate result payload that mirrors gate_artifacts.materialize_artifacts.

    Crucially, this payload uses ``status="completed"`` and does NOT carry a
    top-level ``verdict`` field — i.e. the exact production schema produced by
    scripts/lib/gate_artifacts.py for a successful headless gate execution.
    """
    return {
        "gate": gate,
        "pr_id": pr_id,
        "pr_number": None,
        "status": "completed",
        "summary": f"{gate} execution completed successfully",
        "contract_hash": contract_hash,
        "report_path": report_path,
        "findings": (blocking or []) + (advisory or []),
        "blocking_findings": blocking or [],
        "advisory_findings": advisory or [],
        "required_reruns": [],
        "residual_risk": "",
        "duration_seconds": 12.5,
        "recorded_at": "2026-04-29T10:00:00Z",
    }


class TestClosureVerifierRoundOneCodexFindings:
    """PR #322 codex round-1 findings — production-schema regressions.

    Finding 1: closure verifier must accept the exact result payload produced
    by gate_artifacts (status="completed", no top-level verdict, no
    blocking_count) for both codex_gate and gemini_review.

    Finding 2: closure verifier must locate explicit-absence state for
    claude_github_optional in the requests directory when gate_request_handler
    has not written a result file (its standard path).
    """

    def test_finding_1_codex_completed_payload_passes(self, verifier_env, monkeypatch, tmp_path):
        """Codex gate result with production schema (status=completed) passes."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\n\nclean run\n", encoding="utf-8")
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\n\nno findings\n", encoding="utf-8")

        contract = _make_contract(
            review_stack=["gemini_review", "codex_gate"],
            risk_class="high",
        )
        results_dir = tmp_path / "results"
        _write_gate_result(
            results_dir, "gemini_review", "PR-0",
            _make_gate_artifact_payload(gate="gemini_review", report_path=str(gemini_report)),
        )
        _write_gate_result(
            results_dir, "codex_gate", "PR-0",
            _make_gate_artifact_payload(gate="codex_gate", report_path=str(codex_report)),
        )

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        passed = {c["name"] for c in result["checks"] if c["status"] == "PASS"}
        assert "gate_codex_gate" in passed, [c for c in result["checks"] if "codex" in c["name"]]
        assert "gate_gemini_review" in passed, [c for c in result["checks"] if "gemini" in c["name"]]

    def test_finding_1_codex_completed_with_blocking_finding_fails(
        self, verifier_env, monkeypatch, tmp_path,
    ):
        """status=completed but with a blocking finding still fails — pass requires zero blocking."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        codex_report = reports_dir / "codex.md"
        codex_report.write_text(
            "# Codex Gate\n\n[BLOCKING] something is wrong\n",
            encoding="utf-8",
        )

        contract = _make_contract(
            review_stack=["codex_gate"],
            risk_class="high",
        )
        results_dir = tmp_path / "results"
        _write_gate_result(
            results_dir, "codex_gate", "PR-0",
            _make_gate_artifact_payload(
                gate="codex_gate",
                report_path=str(codex_report),
                blocking=[{"severity": "blocking", "category": "correctness", "message": "x"}],
            ),
        )

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_codex_gate" in failed

    def test_finding_2_claude_github_state_in_requests_dir_passes(
        self, verifier_env, monkeypatch, tmp_path,
    ):
        """Explicit `not_configured` state in requests dir is accepted as evidence."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        # Mirror the production layout: results/ has nothing for the optional
        # gate; requests/ has the contract-driven request payload that
        # gate_request_handler.request_claude_github_with_contract writes.
        results_dir = tmp_path / "review_gates" / "results"
        results_dir.mkdir(parents=True)
        requests_dir = tmp_path / "review_gates" / "requests"
        requests_dir.mkdir(parents=True)
        request_payload = {
            "gate": "claude_github_optional",
            "pr_id": "PR-0",
            "state": "not_configured",
            "contributed_evidence": False,
            "was_intentionally_absent": True,
            "contract_hash": "abcdef1234567890",
            "branch": "feature/demo",
            "review_mode": "per_pr",
        }
        (requests_dir / "pr0-claude_github_optional-contract.json").write_text(
            json.dumps(request_payload), encoding="utf-8",
        )

        contract = _make_contract(review_stack=["claude_github_optional"])

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        claude_check = next(
            c for c in result["checks"] if c["name"] == "gate_claude_github_optional"
        )
        assert claude_check["status"] == "PASS", claude_check

    def test_finding_2_legacy_status_only_request_normalised(
        self, verifier_env, monkeypatch, tmp_path,
    ):
        """Legacy pr-number request payload (status only, no state) is normalised to pass."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        results_dir = tmp_path / "review_gates" / "results"
        results_dir.mkdir(parents=True)
        requests_dir = tmp_path / "review_gates" / "requests"
        requests_dir.mkdir(parents=True)
        # Legacy writer (gate_request_handler._request_claude_github) — only
        # writes `status`, no `state` / `was_intentionally_absent` fields.
        legacy_payload = {
            "gate": "claude_github_optional",
            "status": "configured_dry_run",
            "branch": "feature/demo",
            "pr_number": 45,
        }
        (requests_dir / "pr-45-claude_github_optional.json").write_text(
            json.dumps(legacy_payload), encoding="utf-8",
        )

        contract = _make_contract(review_stack=["claude_github_optional"])

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        claude_check = next(
            c for c in result["checks"] if c["name"] == "gate_claude_github_optional"
        )
        assert claude_check["status"] == "PASS", claude_check

    def test_finding_2_no_request_or_result_still_fails(
        self, verifier_env, monkeypatch, tmp_path,
    ):
        """Total absence of any request or result still fails — fallback is not a free pass."""
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        results_dir = tmp_path / "review_gates" / "results"
        results_dir.mkdir(parents=True)
        # Sibling requests dir does not exist at all.

        contract = _make_contract(review_stack=["claude_github_optional"])

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_claude_github_optional" in failed


# ---------------------------------------------------------------------------
# Round-3 codex finding regressions (PR #301)
# ---------------------------------------------------------------------------


class TestRollupAllGreen:
    """_rollup_all_green must evaluate StatusContext entries, not only CheckRun.

    Earlier logic filtered ``rollup`` to ``__typename == 'CheckRun'`` and used
    ``all(...)`` over the resulting generator. A rollup carrying only
    StatusContext entries (commit statuses) yielded an empty generator, which
    ``all(...)`` evaluates to ``True`` — so failing/pending commit statuses
    were silently treated as passing.
    """

    def test_empty_rollup_is_not_green(self):
        assert cv._rollup_all_green([]) is False

    def test_all_checkruns_success_is_green(self):
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert cv._rollup_all_green(rollup) is True

    def test_failing_checkrun_is_not_green(self):
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        assert cv._rollup_all_green(rollup) is False

    def test_status_context_only_success_is_green(self):
        rollup = [
            {"__typename": "StatusContext", "state": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]
        assert cv._rollup_all_green(rollup) is True

    def test_status_context_only_failure_is_not_green(self):
        # The pre-fix bug: this rollup was treated as green because the
        # CheckRun-only filter produced an empty generator.
        rollup = [
            {"__typename": "StatusContext", "state": "FAILURE"},
        ]
        assert cv._rollup_all_green(rollup) is False

    def test_status_context_pending_is_not_green(self):
        rollup = [
            {"__typename": "StatusContext", "state": "PENDING"},
        ]
        assert cv._rollup_all_green(rollup) is False

    def test_mixed_checkrun_and_status_context_all_green(self):
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]
        assert cv._rollup_all_green(rollup) is True

    def test_mixed_one_failing_status_context_is_not_green(self):
        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "FAILURE"},
        ]
        assert cv._rollup_all_green(rollup) is False

    def test_unknown_typename_is_not_green(self):
        rollup = [
            {"__typename": "MysteryEntry"},
        ]
        assert cv._rollup_all_green(rollup) is False


class TestValidateReviewEvidencePropagatesBranch:
    """`_validate_review_evidence` must forward contract.branch to `_find_gate_result`.

    Without branch propagation, an old result file for the same ``pr_id`` from
    a different branch could satisfy gate checks even though `_find_gate_result`
    has explicit branch filtering for exactly this scenario.
    """

    def test_stale_branch_result_is_rejected(self, verifier_env, monkeypatch, tmp_path):
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        results_dir = tmp_path / "results"
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini\nclean\n", encoding="utf-8")

        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "branch": "feature/old-branch",
            "status": "completed",
            "blocking_findings": [],
            "advisory_findings": [],
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })

        contract = _make_contract(review_stack=["gemini_review"])

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_gemini_review" in failed

    def test_matching_branch_result_is_accepted(self, verifier_env, monkeypatch, tmp_path):
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: _good_pr_payload())

        results_dir = tmp_path / "results"
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini\nclean\n", encoding="utf-8")

        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "branch": "feature/demo",
            "status": "completed",
            "blocking_findings": [],
            "advisory_findings": [],
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })

        contract = _make_contract(review_stack=["gemini_review"])

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        passed = {c["name"] for c in result["checks"] if c["status"] == "PASS"}
        assert "gate_gemini_review" in passed


class TestCLIForwardsBranchAndRequireGithubPr:
    """closure_verifier main() must forward --branch and --require-github-pr to verify_pr_closure.

    Without forwarding, the per-PR CLI path silently skips the GitHub PR / CI
    enforcement added to `verify_pr_closure`, so closure can report PASS
    without checking that a real GitHub PR exists or that CI is green.
    """

    def test_main_forwards_branch_and_require_github_pr(self, verifier_env, monkeypatch):
        captured = {}

        def _fake_verify_pr_closure(**kwargs):
            captured.update(kwargs)
            return {
                "verdict": "pass",
                "mode": "per_pr",
                "pr_id": kwargs["pr_id"],
                "checks": [],
                "review_evidence": None,
            }

        monkeypatch.setattr(cv, "verify_pr_closure", _fake_verify_pr_closure)

        rc = cv.main([
            "--pr-id", "PR-0",
            "--branch", "feature/demo",
            "--require-github-pr",
            "--feature-plan", str(verifier_env["feature_plan"]),
            "--pr-queue", str(verifier_env["pr_queue"]),
            "--json",
        ])
        assert rc == 0
        assert captured["branch"] == "feature/demo"
        assert captured["require_github_pr"] is True
        assert captured["pr_id"] == "PR-0"

    def test_main_default_require_github_pr_is_false(self, verifier_env, monkeypatch):
        captured = {}

        def _fake_verify_pr_closure(**kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "mode": "per_pr", "pr_id": kwargs["pr_id"], "checks": [], "review_evidence": None}

        monkeypatch.setattr(cv, "verify_pr_closure", _fake_verify_pr_closure)

        rc = cv.main([
            "--pr-id", "PR-0",
            "--mode", "post_merge",
            "--feature-plan", str(verifier_env["feature_plan"]),
            "--pr-queue", str(verifier_env["pr_queue"]),
            "--json",
        ])
        assert rc == 0
        assert captured["require_github_pr"] is False
        assert captured["branch"] is None
