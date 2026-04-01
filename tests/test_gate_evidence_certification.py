#!/usr/bin/env python3
"""
PR-2 Certification: Gate Evidence Accuracy And PR-Scoped Lookup.

Gate: gate_pr2_gate_evidence_certification
Contract: docs/core/130_PR_SCOPED_GATE_EVIDENCE_CONTRACT.md

Certifies GE-1 through GE-12 rules by reproducing real scenarios:
  CERT-1: Multi-dispatch PR produces identical provenance (GE-1)
  CERT-2: Cross-PR gate evidence never attributed to wrong PR (GE-2, GE-4, GE-8)
  CERT-3: Branch rejection prevents stale feature results (GE-3)
  CERT-4: Verdict-only report_path enforcement (GE-5, GE-6, GE-7)
  CERT-5: Contract-hash mismatch caught (GE-9)
  CERT-6: GitHub PR missing blocks merge closure (GE-10, GE-12)
  CERT-7: GitHub CI failures block merge closure (GE-11)
  CERT-8: End-to-end clean gate evidence passes closure
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
from queue_reconciler import scan_dispatch_dirs
from review_contract import (
    Deliverable,
    QualityGate,
    ReviewContract,
    TestEvidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FEATURE_PLAN_CONTENT = """\
# Feature: Gate Evidence Certification Feature

**Status**: Active
**Risk-Class**: high

## PR-0: Contract
**Track**: C
**Priority**: P1
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: []

`gate_pr0_contract`

---

## PR-1: Implementation
**Track**: B
**Priority**: P1
**Skill**: @backend-developer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: [PR-0]

`gate_pr1_implementation`

---
"""


def _write_dispatch(path: Path, pr_id: str, dispatch_id: str, track: str = "C") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[[TARGET:{track}]]\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n",
        encoding="utf-8",
    )


def _write_receipt(receipts_file: Path, dispatch_id: str) -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"dispatch_id": dispatch_id, "event_type": "task_complete", "status": "success"}
    with receipts_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_gate_result(results_dir: Path, filename: str, data: Dict[str, Any]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    fp = results_dir / filename
    fp.write_text(json.dumps(data), encoding="utf-8")
    return fp


def _write_report(reports_dir: Path, filename: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    fp = reports_dir / filename
    fp.write_text("# Report\nTest report content.\n", encoding="utf-8")
    return fp


def _make_contract(
    pr_id: str = "PR-0",
    review_stack: Optional[List[str]] = None,
    content_hash: str = "test-hash-abc123",
    branch: str = "feature/test",
) -> ReviewContract:
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Test PR",
        feature_title="Gate Evidence Certification",
        branch=branch,
        track="C",
        risk_class="high",
        merge_policy="human",
        review_stack=review_stack or ["gemini_review", "codex_gate"],
        closure_stage="in_review",
        deliverables=[Deliverable(description="test", category="implementation")],
        non_goals=[],
        scope_files=[],
        changed_files=[],
        quality_gate=QualityGate(gate_id="gate_pr0_contract", checks=["check 1"]),
        test_evidence=TestEvidence(
            test_files=["tests/test.py"],
            test_command="pytest tests/test.py",
        ),
        deterministic_findings=[],
        content_hash=content_hash,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    dispatch_dir.mkdir(parents=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True)
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True)

    (project_root / "FEATURE_PLAN.md").write_text(FEATURE_PLAN_CONTENT, encoding="utf-8")

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))

    return {
        "project_root": project_root,
        "data_dir": data_dir,
        "dispatch_dir": dispatch_dir,
        "state_dir": state_dir,
        "results_dir": results_dir,
        "reports_dir": reports_dir,
        "receipts": state_dir / "t0_receipts.ndjson",
        "feature_plan": project_root / "FEATURE_PLAN.md",
    }


# ===========================================================================
# CERT-1: Multi-dispatch PR deterministic provenance (GE-1)
# ===========================================================================

class TestCert1DeterministicProvenance:
    """Certify that multiple dispatches per PR produce identical ordering."""

    def test_cert1a_repeated_runs_same_order(self, env):
        """10 consecutive scans produce identical ordering."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "20260401-120000-contract-C.md", "PR-0", "d-first")
        _write_dispatch(dd / "completed" / "20260401-090000-contract-C.md", "PR-0", "d-second")
        _write_dispatch(dd / "completed" / "20260401-150000-contract-C.md", "PR-0", "d-third")

        results = [
            [r.dispatch_id for r in scan_dispatch_dirs(dd)]
            for _ in range(10)
        ]

        assert all(r == results[0] for r in results), "All 10 scans must produce identical order"

    def test_cert1b_chronological_sort(self, env):
        """Dispatches are sorted by timestamp prefix (earliest first)."""
        dd = env["dispatch_dir"]
        # dispatch_id = f.stem (filename without .md)
        _write_dispatch(dd / "active" / "20260401-150000-late-C.md", "PR-0", "20260401-150000-late-C")
        _write_dispatch(dd / "active" / "20260401-090000-early-C.md", "PR-0", "20260401-090000-early-C")
        _write_dispatch(dd / "active" / "20260401-120000-mid-C.md", "PR-0", "20260401-120000-mid-C")

        records = scan_dispatch_dirs(dd)
        ids = [r.dispatch_id for r in records]

        assert ids == [
            "20260401-090000-early-C",
            "20260401-120000-mid-C",
            "20260401-150000-late-C",
        ], "Must be sorted chronologically by timestamp prefix"

    def test_cert1c_multiple_dirs_sorted_within_each(self, env):
        """Sorting applies within each dispatch state directory."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "active" / "20260401-120000-a-C.md", "PR-0", "20260401-120000-a-C")
        _write_dispatch(dd / "active" / "20260401-090000-a-C.md", "PR-0", "20260401-090000-a-C")
        _write_dispatch(dd / "completed" / "20260401-080000-c-C.md", "PR-0", "20260401-080000-c-C")
        _write_dispatch(dd / "completed" / "20260401-100000-c-C.md", "PR-0", "20260401-100000-c-C")

        records = scan_dispatch_dirs(dd)
        active = [r.dispatch_id for r in records if r.dir_state == "active"]
        completed = [r.dispatch_id for r in records if r.dir_state == "completed"]

        assert active == ["20260401-090000-a-C", "20260401-120000-a-C"]
        assert completed == ["20260401-080000-c-C", "20260401-100000-c-C"]


# ===========================================================================
# CERT-2: Cross-PR gate evidence isolation (GE-2, GE-4, GE-8)
# ===========================================================================

class TestCert2CrossPrIsolation:
    """Certify that gate results never leak across PRs."""

    def test_cert2a_contract_path_wrong_pr_rejected(self, env):
        """Contract file with wrong pr_id in JSON is rejected (GE-4)."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review",
            "pr_id": "PR-1",  # WRONG — file says pr0 but JSON says PR-1
            "status": "pass",
            "contract_hash": "test-hash",
        })

        result = cv._find_gate_result("gemini_review", "PR-0", env["results_dir"])
        assert result is None, "Must not match when JSON pr_id differs from queried PR"

    def test_cert2b_legacy_path_wrong_pr_rejected(self, env):
        """Legacy glob file with wrong pr_id is rejected (GE-2 AND logic)."""
        _write_gate_result(env["results_dir"], "pr-1-gemini_review.json", {
            "gate": "gemini_review",
            "pr_id": "PR-1",
            "status": "pass",
        })

        result = cv._find_gate_result("gemini_review", "PR-0", env["results_dir"])
        assert result is None, "Legacy AND logic must reject pr_id mismatch"

    def test_cert2c_correct_pr_accepted(self, env):
        """Matching pr_id and gate returns the result."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review",
            "pr_id": "PR-0",
            "status": "pass",
            "contract_hash": "test-hash",
        })

        result = cv._find_gate_result("gemini_review", "PR-0", env["results_dir"])
        assert result is not None
        assert result["pr_id"] == "PR-0"

    def test_cert2d_two_prs_same_gate_isolated(self, env):
        """PR-0 and PR-1 each have gemini_review — results don't cross."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
        })
        _write_gate_result(env["results_dir"], "pr1-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-1", "status": "fail",
        })

        r0 = cv._find_gate_result("gemini_review", "PR-0", env["results_dir"])
        r1 = cv._find_gate_result("gemini_review", "PR-1", env["results_dir"])

        assert r0["status"] == "pass", "PR-0 gets its own result"
        assert r1["status"] == "fail", "PR-1 gets its own result"


# ===========================================================================
# CERT-3: Branch rejection for stale feature results (GE-3)
# ===========================================================================

class TestCert3BranchRejection:
    """Certify that stale results from other features are rejected."""

    def test_cert3a_wrong_branch_rejected(self, env):
        """Result from old feature branch is rejected when current branch differs."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "branch": "feature/old-feature",
        })

        result = cv._find_gate_result(
            "gemini_review", "PR-0", env["results_dir"],
            branch="feature/current-feature",
        )
        assert result is None, "Must reject result from different branch"

    def test_cert3b_matching_branch_accepted(self, env):
        """Result from correct branch is accepted."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "branch": "feature/current",
        })

        result = cv._find_gate_result(
            "gemini_review", "PR-0", env["results_dir"],
            branch="feature/current",
        )
        assert result is not None


# ===========================================================================
# CERT-4: Verdict-only report_path enforcement (GE-5, GE-6, GE-7)
# ===========================================================================

class TestCert4ReportPathEnforcement:
    """Certify that terminal verdicts require valid report_path."""

    def test_cert4a_pass_without_report_path_fails(self, env):
        """Gate result with status=pass but no report_path fails validation (GE-5)."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "contract_hash": "test-hash-abc123",
        })
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "pass",
            "contract_hash": "test-hash-abc123",
        })

        contract = _make_contract()
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        report_checks = [c for c in checks if c.name.startswith("report_")]
        failed = [c for c in report_checks if c.status == "FAIL"]

        assert len(failed) >= 1, "Must fail when report_path is missing"

    def test_cert4b_nonexistent_report_file_fails(self, env):
        """Gate result pointing to non-existent file fails (GE-6)."""
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "report_path": "/nonexistent/path/report.md",
            "contract_hash": "test-hash-abc123",
        })
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "pass",
            "report_path": "/nonexistent/path/report2.md",
            "contract_hash": "test-hash-abc123",
        })

        contract = _make_contract()
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        report_checks = [c for c in checks if c.name.startswith("report_")]
        failed = [c for c in report_checks if c.status == "FAIL"]

        assert len(failed) >= 1, "Must fail when report file doesn't exist"

    def test_cert4c_codex_verdict_triggers_enforcement(self, env):
        """Codex verdict=pass (not status) still triggers report_path check (GE-7)."""
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0",
            "status": "requested",  # non-terminal
            "verdict": "pass",       # terminal — must trigger enforcement
            "contract_hash": "test-hash-abc123",
        })
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "report_path": str(_write_report(env["reports_dir"], "gemini.md")),
            "contract_hash": "test-hash-abc123",
        })

        contract = _make_contract()
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        codex_report = [c for c in checks if "codex" in c.name and c.status == "FAIL"]

        assert len(codex_report) >= 1, "Codex verdict=pass must trigger report_path enforcement"

    def test_cert4d_valid_report_path_passes(self, env):
        """Gate result with valid report_path to existing file passes."""
        report = _write_report(env["reports_dir"], "gemini-report.md")
        report2 = _write_report(env["reports_dir"], "codex-report.md")

        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "report_path": str(report), "contract_hash": "test-hash-abc123",
        })
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "pass",
            "report_path": str(report2), "contract_hash": "test-hash-abc123",
        })

        contract = _make_contract()
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        report_checks = [c for c in checks if c.name.startswith("report_")]
        failed = [c for c in report_checks if c.status == "FAIL"]

        assert len(failed) == 0, f"Valid report_path should pass, got failures: {failed}"


# ===========================================================================
# CERT-5: Contract-hash mismatch (GE-9)
# ===========================================================================

class TestCert5ContractHashMismatch:
    """Certify that stale contract hashes are caught."""

    def test_cert5a_mismatched_hash_fails(self, env):
        """Gate result with wrong contract_hash is flagged."""
        report = _write_report(env["reports_dir"], "report.md")
        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "report_path": str(report), "contract_hash": "WRONG-HASH",
        })
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "pass",
            "report_path": str(report), "contract_hash": "test-hash-abc123",
        })

        contract = _make_contract(content_hash="test-hash-abc123")
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        hash_fails = [c for c in checks if "hash" in c.name.lower() and c.status == "FAIL"]

        assert len(hash_fails) >= 1, "Mismatched contract_hash must fail"


# ===========================================================================
# CERT-6: GitHub PR missing blocks merge closure (GE-10, GE-12)
# ===========================================================================

class TestCert6GithubPrRequired:
    """Certify that local-only closure is blocked without GitHub PR."""

    def test_cert6a_no_github_pr_blocks_closure(self, env):
        """verify_pr_closure fails when no GitHub PR exists (GE-12)."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "d-001.md", "PR-0", "d-001")
        _write_receipt(env["receipts"], "d-001")

        with patch.object(cv, "_find_branch_pr", return_value=None):
            result = cv.verify_pr_closure(
                pr_id="PR-0",
                project_root=env["project_root"],
                feature_plan=env["feature_plan"],
                dispatch_dir=dd,
                receipts_file=env["receipts"],
                state_dir=env["state_dir"],
                branch="feature/test",
                require_github_pr=True,
            )

        checks_by_name = {c["name"]: c["status"] for c in result["checks"]}
        assert checks_by_name.get("github_pr_exists") == "FAIL"
        assert result["verdict"] == "fail"

    def test_cert6b_github_pr_not_checked_without_flag(self, env):
        """Without require_github_pr=True, GitHub check is skipped (backward compat)."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "d-001.md", "PR-0", "d-001")
        _write_receipt(env["receipts"], "d-001")

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=dd,
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
        )

        checks_by_name = {c["name"]: c["status"] for c in result["checks"]}
        assert "github_pr_exists" not in checks_by_name


# ===========================================================================
# CERT-7: GitHub CI failures block merge (GE-11)
# ===========================================================================

class TestCert7GithubCiRequired:
    """Certify that failing CI blocks merge closure."""

    def test_cert7a_failing_checks_block(self, env):
        """GitHub PR with failing CI check blocks merge readiness."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "d-001.md", "PR-0", "d-001")
        _write_receipt(env["receipts"], "d-001")

        mock_pr = {
            "number": 42, "state": "OPEN", "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
        }
        with patch.object(cv, "_find_branch_pr", return_value=mock_pr):
            result = cv.verify_pr_closure(
                pr_id="PR-0",
                project_root=env["project_root"],
                feature_plan=env["feature_plan"],
                dispatch_dir=dd,
                receipts_file=env["receipts"],
                state_dir=env["state_dir"],
                branch="feature/test",
                require_github_pr=True,
            )

        checks_by_name = {c["name"]: c["status"] for c in result["checks"]}
        assert checks_by_name.get("github_pr_exists") == "PASS"
        assert checks_by_name.get("github_checks") == "FAIL"

    def test_cert7b_empty_rollup_fails(self, env):
        """Empty statusCheckRollup (no CI configured) fails (GE-11)."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "d-001.md", "PR-0", "d-001")
        _write_receipt(env["receipts"], "d-001")

        mock_pr = {
            "number": 42, "state": "OPEN", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        with patch.object(cv, "_find_branch_pr", return_value=mock_pr):
            result = cv.verify_pr_closure(
                pr_id="PR-0",
                project_root=env["project_root"],
                feature_plan=env["feature_plan"],
                dispatch_dir=dd,
                receipts_file=env["receipts"],
                state_dir=env["state_dir"],
                branch="feature/test",
                require_github_pr=True,
            )

        checks_by_name = {c["name"]: c["status"] for c in result["checks"]}
        assert checks_by_name.get("github_checks") == "FAIL", "Empty CI rollup must fail"

    def test_cert7c_green_checks_pass(self, env):
        """All green CI checks pass."""
        dd = env["dispatch_dir"]
        _write_dispatch(dd / "completed" / "d-001.md", "PR-0", "d-001")
        _write_receipt(env["receipts"], "d-001")

        mock_pr = {
            "number": 42, "state": "OPEN", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        }
        with patch.object(cv, "_find_branch_pr", return_value=mock_pr):
            result = cv.verify_pr_closure(
                pr_id="PR-0",
                project_root=env["project_root"],
                feature_plan=env["feature_plan"],
                dispatch_dir=dd,
                receipts_file=env["receipts"],
                state_dir=env["state_dir"],
                branch="feature/test",
                require_github_pr=True,
            )

        checks_by_name = {c["name"]: c["status"] for c in result["checks"]}
        assert checks_by_name.get("github_pr_exists") == "PASS"
        assert checks_by_name.get("github_checks") == "PASS"


# ===========================================================================
# CERT-8: End-to-end clean gate evidence
# ===========================================================================

class TestCert8EndToEnd:
    """Certify the full happy path with all evidence correctly scoped."""

    def test_cert8a_all_evidence_aligned_passes(self, env):
        """Complete gate evidence with matching pr_id, hash, report_path passes."""
        report1 = _write_report(env["reports_dir"], "gemini-report.md")
        report2 = _write_report(env["reports_dir"], "codex-report.md")

        _write_gate_result(env["results_dir"], "pr0-gemini_review-contract.json", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "report_path": str(report1), "contract_hash": "test-hash-abc123",
            "branch": "feature/test",
        })
        _write_gate_result(env["results_dir"], "pr0-codex_gate-contract.json", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "pass",
            "verdict": "pass",  # Codex uses verdict field for gate check
            "report_path": str(report2), "contract_hash": "test-hash-abc123",
            "branch": "feature/test",
        })

        contract = _make_contract()
        checks = cv._validate_review_evidence(contract, env["results_dir"])
        failed = [c for c in checks if c.status == "FAIL"]

        assert len(failed) == 0, f"All evidence aligned — no failures expected, got: {[f'{c.name}: {c.detail}' for c in failed]}"
