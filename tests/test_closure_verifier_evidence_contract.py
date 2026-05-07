#!/usr/bin/env python3
"""Phase 1.5 PR-1: evidence-contract enforcement tests for closure_verifier.

Covers 4 findings from the 2026-05-06 codex re-audit (OI-1317, OI-1322):

1. Standard status="completed"/"approve"/"failed" gate receipt with missing report_path
   → gate rejected (not just the separate report_{gate} loop, but gate_{gate} itself)
2. Standard receipt with status="completed" and [BLOCKING] markers in report
   → contradiction flagged
3. claude_github_optional request from a different branch → ignored
4. statusCheckRollup with StatusContext-only failures → github_checks=fail
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


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

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


def _make_contract(
    pr_id="PR-0",
    review_stack=None,
    risk_class="medium",
    branch="feature/demo",
    content_hash="abcdef1234567890",
):
    if review_stack is None:
        review_stack = ["gemini_review"]
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Demo PR",
        feature_title="Demo Feature",
        branch=branch,
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


def _write_gate_result(results_dir: Path, gate: str, pr_id: str, data: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    pr_slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{pr_slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_request(results_dir: Path, gate: str, pr_id: str, data: dict) -> Path:
    requests_dir = results_dir.parent / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    pr_slug = pr_id.lower().replace("-", "")
    path = requests_dir / f"{pr_slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _good_pr_payload_checkrun():
    return {
        "number": 45,
        "url": "https://example.test/pr/45",
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "mergeCommit": {"oid": "abc123"},
    }


# ---------------------------------------------------------------------------
# Finding 1: Standard terminal receipt missing report_path → gate rejected
# ---------------------------------------------------------------------------

class TestReportPathEnforcementStandardReceipt:
    """OI-1317 / OI-1322 — Finding 1.

    Standard gate receipts with status in {completed, approve, failed}
    must enforce report_path in the gate_{gate} handler itself, not only
    via the deferred report_{gate} loop.  A receipt that claims "completed"
    with no report_path must cause gate_{gate}=FAIL directly.
    """

    def test_completed_receipt_without_report_path_rejected(self, tmp_path):
        """gemini_review with status=completed but missing report_path → gate_gemini_review=FAIL."""
        results_dir = tmp_path / "results"
        contract = _make_contract(review_stack=["gemini_review"])

        # status="completed" is a PASS_STATE in gate_status.py — gate_is_pass returns True
        # but without report_path the gate must reject.
        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "completed",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            # report_path intentionally absent
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert "gate_gemini_review" in check_map
        assert check_map["gate_gemini_review"].status == "FAIL"
        assert "report_path" in check_map["gate_gemini_review"].detail

    def test_approve_receipt_without_report_path_rejected(self, tmp_path):
        """gemini_review with status=approve but missing report_path → gate_gemini_review=FAIL."""
        results_dir = tmp_path / "results"
        contract = _make_contract(review_stack=["gemini_review"])

        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "approve",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert check_map["gate_gemini_review"].status == "FAIL"

    def test_completed_receipt_with_report_path_passes(self, tmp_path):
        """gemini_review with status=completed and valid report_path → gate_gemini_review=PASS."""
        results_dir = tmp_path / "results"
        report_file = tmp_path / "gemini_report.md"
        report_file.write_text("# Gemini Review\nAll clear.\n", encoding="utf-8")

        contract = _make_contract(review_stack=["gemini_review"])
        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "completed",
            "blocking_count": 0,
            "advisory_count": 1,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert check_map["gate_gemini_review"].status == "PASS"


# ---------------------------------------------------------------------------
# Finding 2: Completed receipt with [BLOCKING] markers → contradiction flagged
# ---------------------------------------------------------------------------

class TestGateReportContradictionCompletedStatus:
    """OI-1317 — Finding 2.

    When a gate result has status="completed" (a PASS_STATE) but the
    normalized report contains [BLOCKING] markers, _detect_gate_report_contradictions
    must flag contradiction_{gate}=FAIL.

    The old code used _gate_terminal_status which returns "" for status="completed"
    (only recognizes "pass"/"fail") so the contradiction check silently passed.
    """

    def test_completed_status_with_blocking_markers_flagged(self, tmp_path):
        """status=completed + [BLOCKING] in report → contradiction_gemini_review=FAIL."""
        results_dir = tmp_path / "results"

        # Report file contains a [BLOCKING] marker
        report_file = tmp_path / "gemini_report.md"
        report_file.write_text(
            "# Gemini Review\n\n[BLOCKING] Critical security flaw found.\n",
            encoding="utf-8",
        )

        contract = _make_contract(review_stack=["gemini_review"])
        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "completed",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._detect_gate_report_contradictions(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert "contradiction_gemini_review" in check_map
        assert check_map["contradiction_gemini_review"].status == "FAIL"
        assert "blocking indicator" in check_map["contradiction_gemini_review"].detail

    def test_pass_status_with_blocking_markers_also_flagged(self, tmp_path):
        """status=pass + [BLOCKING] in report → contradiction flagged (existing behavior preserved)."""
        results_dir = tmp_path / "results"

        report_file = tmp_path / "gemini_report.md"
        report_file.write_text("# Gemini\n[BLOCKING] found.\n", encoding="utf-8")

        contract = _make_contract(review_stack=["gemini_review"])
        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "blocking_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._detect_gate_report_contradictions(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert check_map["contradiction_gemini_review"].status == "FAIL"

    def test_completed_status_clean_report_no_contradiction(self, tmp_path):
        """status=completed + clean report → no contradiction."""
        results_dir = tmp_path / "results"

        report_file = tmp_path / "gemini_report.md"
        report_file.write_text("# Gemini Review\nAll clear, 0 issues.\n", encoding="utf-8")

        contract = _make_contract(review_stack=["gemini_review"])
        gemini_result = {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "completed",
            "blocking_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
        }
        _write_gate_result(results_dir, "gemini_review", "PR-0", gemini_result)

        checks = cv._detect_gate_report_contradictions(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert check_map["contradiction_gemini_review"].status == "PASS"


# ---------------------------------------------------------------------------
# Finding 3: Cross-branch claude_github_optional request → ignored
# ---------------------------------------------------------------------------

class TestCrossBranchRequestFilter:
    """OI-1322 — Finding 3.

    _find_gate_request_payload previously had no branch filter, so a
    claude_github_optional request recorded for an old branch (same pr_id)
    could be returned as evidence for the current branch's closure.

    After the fix, requests from a different branch must be ignored, causing
    gate_claude_github_optional=FAIL when no same-branch evidence exists.
    """

    def test_cross_branch_request_not_accepted_as_evidence(self, tmp_path):
        """Request from feature/old-pr with same pr_id must not satisfy feature/new-pr closure."""
        results_dir = tmp_path / "results"

        # Request recorded for a DIFFERENT branch
        cross_branch_request = {
            "gate": "claude_github_optional",
            "pr_id": "PR-0",
            "branch": "feature/old-pr",
            "state": "not_configured",
            "was_intentionally_absent": True,
            "contributed_evidence": False,
        }
        _write_request(results_dir, "claude_github_optional", "PR-0", cross_branch_request)

        # Contract is for the CURRENT branch feature/new-pr
        contract = _make_contract(
            review_stack=["claude_github_optional"],
            branch="feature/new-pr",
        )

        checks = cv._validate_review_evidence(contract, results_dir, branch="feature/new-pr")
        check_map = {c.name: c for c in checks}

        assert "gate_claude_github_optional" in check_map
        # Cross-branch request must be ignored → no evidence → FAIL
        assert check_map["gate_claude_github_optional"].status == "FAIL"

    def test_same_branch_request_accepted(self, tmp_path):
        """Request from the same branch is accepted as valid evidence."""
        results_dir = tmp_path / "results"

        same_branch_request = {
            "gate": "claude_github_optional",
            "pr_id": "PR-0",
            "branch": "feature/current-pr",
            "state": "not_configured",
            "was_intentionally_absent": True,
            "contributed_evidence": False,
        }
        _write_request(results_dir, "claude_github_optional", "PR-0", same_branch_request)

        contract = _make_contract(
            review_stack=["claude_github_optional"],
            branch="feature/current-pr",
        )

        checks = cv._validate_review_evidence(contract, results_dir, branch="feature/current-pr")
        check_map = {c.name: c for c in checks}

        assert check_map["gate_claude_github_optional"].status == "PASS"

    def test_branchless_request_not_filtered(self, tmp_path):
        """Request without a branch field is not filtered (legacy compatibility)."""
        results_dir = tmp_path / "results"

        branchless_request = {
            "gate": "claude_github_optional",
            "pr_id": "PR-0",
            # no "branch" key
            "state": "not_configured",
            "was_intentionally_absent": True,
            "contributed_evidence": False,
        }
        _write_request(results_dir, "claude_github_optional", "PR-0", branchless_request)

        contract = _make_contract(
            review_stack=["claude_github_optional"],
            branch="feature/any-branch",
        )

        checks = cv._validate_review_evidence(contract, results_dir, branch="feature/any-branch")
        check_map = {c.name: c for c in checks}

        # Legacy request without branch field must not be filtered
        assert check_map["gate_claude_github_optional"].status == "PASS"


# ---------------------------------------------------------------------------
# Finding 4: StatusContext-only rollup with failures → github_checks=fail
# ---------------------------------------------------------------------------

class TestStatusContextRollupEvaluation:
    """OI-1322 — Finding 4.

    _rollup_all_green must treat StatusContext failures as blocking.
    The original code was CheckRun-only and would silently treat a
    StatusContext-only rollup as all-green (empty CheckRun generator).
    """

    def test_status_context_failure_marks_github_checks_fail(self, verifier_env, monkeypatch, tmp_path):
        """StatusContext-only rollup with state=FAILURE → github_checks=FAIL."""
        failing_rollup_pr = {
            "number": 42,
            "url": "https://example.test/pr/42",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"__typename": "StatusContext", "state": "FAILURE", "context": "ci/jenkins"},
            ],
            "mergeCommit": {"oid": "abc123"},
        }
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: failing_rollup_pr)

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
        )

        check_map = {c["name"]: c for c in result["checks"]}
        assert "github_checks" in check_map
        assert check_map["github_checks"]["status"] == "FAIL"

    def test_status_context_success_marks_github_checks_pass(self, verifier_env, monkeypatch, tmp_path):
        """StatusContext-only rollup with state=SUCCESS → github_checks=PASS."""
        passing_rollup_pr = {
            "number": 43,
            "url": "https://example.test/pr/43",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"__typename": "StatusContext", "state": "SUCCESS", "context": "ci/jenkins"},
            ],
            "mergeCommit": {"oid": "def456"},
        }
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: passing_rollup_pr)

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
        )

        check_map = {c["name"]: c for c in result["checks"]}
        assert check_map["github_checks"]["status"] == "PASS"

    def test_mixed_checkrun_and_status_context_failure_fails(self, verifier_env, monkeypatch):
        """Mixed rollup where CheckRun passes but StatusContext fails → github_checks=FAIL."""
        mixed_rollup_pr = {
            "number": 44,
            "url": "https://example.test/pr/44",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "StatusContext", "state": "FAILURE", "context": "deploy/staging"},
            ],
            "mergeCommit": {"oid": "ghi789"},
        }
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, p: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: mixed_rollup_pr)

        result = cv.verify_closure(
            project_root=verifier_env["project_root"],
            feature_plan=verifier_env["feature_plan"],
            pr_queue=verifier_env["pr_queue"],
            branch="feature/demo",
            mode="pre_merge",
            claim_file=verifier_env["claim_file"],
        )

        check_map = {c["name"]: c for c in result["checks"]}
        assert check_map["github_checks"]["status"] == "FAIL"

    def test_rollup_all_green_statuscontext_unit(self):
        """Unit test: _rollup_all_green returns False for StatusContext with non-SUCCESS state."""
        assert cv._rollup_all_green([
            {"__typename": "StatusContext", "state": "FAILURE"},
        ]) is False

        assert cv._rollup_all_green([
            {"__typename": "StatusContext", "state": "PENDING"},
        ]) is False

        assert cv._rollup_all_green([
            {"__typename": "StatusContext", "state": "SUCCESS"},
        ]) is True

        assert cv._rollup_all_green([
            {"__typename": "StatusContext", "state": "SUCCESS"},
            {"__typename": "StatusContext", "state": "FAILURE"},
        ]) is False


# ---------------------------------------------------------------------------
# OI-1338: report_path enforcement on codex_gate handler
# ---------------------------------------------------------------------------

class TestReportPathEnforcementCodexGate:
    """OI-1338 — codex_gate must enforce report_path on terminal-status receipts.

    Mirrors TestReportPathEnforcementStandardReceipt but for the codex_gate
    handler.  Previously only the gemini_review handler checked report_path;
    codex_gate silently skipped the check and let a terminal result without
    evidence pass through to gate_is_pass.
    """

    def test_missing_report_path_on_terminal_status_fails(self, tmp_path):
        """codex_gate with terminal status=pass but no report_path → gate_codex_gate=FAIL."""
        results_dir = tmp_path / "results"
        # review_stack=["codex_gate"] + risk_class="medium" → enforcement.required=True
        # (reason: codex_gate_in_review_stack, risk != low)
        contract = _make_contract(review_stack=["codex_gate"], risk_class="medium")

        codex_result = {
            "gate": "codex_gate",
            "pr_id": "PR-0",
            "status": "pass",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            # report_path intentionally absent
        }
        _write_gate_result(results_dir, "codex_gate", "PR-0", codex_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert "gate_codex_gate" in check_map
        assert check_map["gate_codex_gate"].status == "FAIL"
        assert "report_path" in check_map["gate_codex_gate"].detail

    def test_with_report_path_passes(self, tmp_path):
        """codex_gate with terminal status=pass and valid report_path → gate_codex_gate=PASS."""
        results_dir = tmp_path / "results"
        report_file = tmp_path / "codex_report.md"
        report_file.write_text("# Codex Review\nAll clear.\n", encoding="utf-8")

        contract = _make_contract(review_stack=["codex_gate"], risk_class="medium")
        codex_result = {
            "gate": "codex_gate",
            "pr_id": "PR-0",
            "status": "pass",
            "blocking_count": 0,
            "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
        }
        _write_gate_result(results_dir, "codex_gate", "PR-0", codex_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert "gate_codex_gate" in check_map
        assert check_map["gate_codex_gate"].status == "PASS"

    def test_non_terminal_status_doesnt_apply(self, tmp_path):
        """codex_gate with non-terminal status=pending and no report_path → not a report_path FAIL.

        report_path enforcement only applies when the result has a terminal
        status (pass/fail/completed/approve/etc.).  A pending result is
        non-terminal so the enforcement does not trigger; the gate fails for a
        different reason (incomplete/pending status).
        """
        results_dir = tmp_path / "results"
        contract = _make_contract(review_stack=["codex_gate"], risk_class="medium")

        codex_result = {
            "gate": "codex_gate",
            "pr_id": "PR-0",
            "status": "pending",
            "blocking_count": 0,
            # no report_path — must NOT be the reason for failure
        }
        _write_gate_result(results_dir, "codex_gate", "PR-0", codex_result)

        checks = cv._validate_review_evidence(contract, results_dir, branch=contract.branch)
        check_map = {c.name: c for c in checks}

        assert "gate_codex_gate" in check_map
        # Gate fails (pending is not passing), but NOT because of missing report_path
        assert check_map["gate_codex_gate"].status == "FAIL"
        assert "report_path" not in check_map["gate_codex_gate"].detail
