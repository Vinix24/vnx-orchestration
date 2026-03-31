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
