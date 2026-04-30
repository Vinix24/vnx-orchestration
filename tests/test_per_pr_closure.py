#!/usr/bin/env python3
"""Tests for per-PR closure verification and gate evidence contradiction detection (PR-2).

Covers:
- Per-PR closure mode works without requiring whole-feature completion
- Per-PR closure uses reconciled queue state
- Contradictory gate result JSON vs report content fails explicitly
- Stale queue state is caught during per-PR closure
- Gate evidence mismatch surfaces as explicit failure
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

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


FEATURE_PLAN = """\
# Feature: Test Per-PR Closure

**Status**: Active
**Risk-Class**: high

## PR-0: Foundation
**Track**: C
**Priority**: P1
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: []

`gate_pr0_foundation`

---

## PR-1: Core
**Track**: B
**Priority**: P1
**Skill**: @backend-developer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: [PR-0]

`gate_pr1_core`

---

## PR-2: Integration
**Track**: C
**Priority**: P2
**Skill**: @quality-engineer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: [PR-1]

`gate_pr2_integration`

---
"""


def _write_dispatch(path: Path, pr_id: str, dispatch_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[[TARGET:B]]\nManager Block\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n"
    )


def _write_receipt(receipts_file: Path, dispatch_id: str) -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"dispatch_id": dispatch_id, "event_type": "task_complete", "status": "success"}
    with receipts_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _make_contract(
    pr_id="PR-0",
    review_stack=None,
    risk_class="high",
    deterministic_findings=None,
    content_hash="abcdef1234567890",
):
    if review_stack is None:
        review_stack = ["gemini_review", "codex_gate"]
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Test PR",
        feature_title="Test Feature",
        branch="feature/test",
        track="C",
        risk_class=risk_class,
        merge_policy="human",
        review_stack=list(review_stack),
        closure_stage="in_review",
        deliverables=[Deliverable(description="test", category="implementation")],
        non_goals=[],
        scope_files=[],
        changed_files=[],
        quality_gate=QualityGate(gate_id="gate_test", checks=["check 1"]),
        test_evidence=TestEvidence(test_files=["tests/test_demo.py"], test_command="pytest"),
        deterministic_findings=deterministic_findings or [],
        content_hash=content_hash,
    )


def _write_gate_result(results_dir: Path, gate: str, pr_id: str, data: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    pr_slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{pr_slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def closure_env(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    project_root.mkdir()

    fp = project_root / "FEATURE_PLAN.md"
    fp.write_text(FEATURE_PLAN)

    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    dispatch_dir.mkdir(parents=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    receipts = state_dir / "t0_receipts.ndjson"

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

    return {
        "project_root": project_root,
        "feature_plan": fp,
        "dispatch_dir": dispatch_dir,
        "state_dir": state_dir,
        "receipts_file": receipts,
    }


# ---------------------------------------------------------------------------
# Per-PR closure mode tests
# ---------------------------------------------------------------------------


class TestPerPRClosure:
    def test_completed_pr_passes_without_feature_completion(self, closure_env, tmp_path):
        """Per-PR closure passes for a completed PR even when feature is not done."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })
        _write_gate_result(results_dir, "codex_gate", "PR-0", {
            "gate": "codex_gate", "pr_id": "PR-0", "verdict": "pass",
            "required": True, "contract_hash": "abcdef1234567890",
            "content_hash": "abcdef1234567890",
            "report_path": str(codex_report),
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "pass"
        assert result["mode"] == "per_pr"
        assert result["pr_id"] == "PR-0"

    def test_incomplete_pr_fails(self, closure_env):
        """Per-PR closure fails when PR is not yet completed."""
        # PR-0 has no dispatches — state is pending
        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "pr_completed" in failed

    def test_unknown_pr_fails(self, closure_env):
        """Per-PR closure fails for PR not in feature plan."""
        result = cv.verify_pr_closure(
            pr_id="PR-99",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "pr_exists_in_plan" in failed

    def test_no_review_contract_fails(self, closure_env):
        """Per-PR closure requires a review contract."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=None,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "review_contract" in failed

    def test_blocking_drift_detected_during_per_pr_closure(self, closure_env):
        """Per-PR closure catches stale queue state via reconciliation."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "active" / "d0.md", "PR-0", "d0"
        )
        # Write stale projection showing PR-0 as pending
        state_dir = closure_env["state_dir"]
        (state_dir / "pr_queue_state.json").write_text(json.dumps({
            "prs": [{"id": "PR-0", "status": "queued"}],
            "completed": [], "active": [], "blocked": [],
        }))

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
        )

        assert result["verdict"] == "fail"
        # PR-0 is active not completed
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "pr_completed" in failed


# ---------------------------------------------------------------------------
# Gate evidence contradiction tests
# ---------------------------------------------------------------------------


class TestGateReportContradiction:
    def test_pass_gate_with_blocking_report_fails(self, closure_env, tmp_path):
        """Gate says pass but report has blocking findings — contradiction detected."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text(
            "# Gemini Review\n\n"
            "## Findings\n"
            "- [BLOCKING] Missing error handling in queue_reconciler.py\n"
            "- [BLOCKING] Race condition in dispatch scanner\n"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "contradiction_gemini_review" in failed
        contradiction_check = next(c for c in result["checks"] if c["name"] == "contradiction_gemini_review")
        assert "evidence mismatch" in contradiction_check["detail"]

    def test_fail_gate_with_clean_report_fails(self, closure_env, tmp_path):
        """Gate says fail but report has no blocking findings — contradiction detected."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text(
            "# Gemini Review\n\n"
            "All clear. No issues found.\n"
            "Advisory: consider adding more documentation.\n"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "fail",
            "blocking_count": 3, "advisory_count": 1,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "contradiction_gemini_review" in failed

    def test_consistent_gate_and_report_passes(self, closure_env, tmp_path):
        """Gate and report agree — no contradiction."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\n\nAll clear. No blocking issues.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\n\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })
        _write_gate_result(results_dir, "codex_gate", "PR-0", {
            "gate": "codex_gate", "pr_id": "PR-0", "verdict": "pass",
            "required": True, "contract_hash": "abcdef1234567890",
            "content_hash": "abcdef1234567890",
            "report_path": str(codex_report),
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "pass"
        contradiction_checks = [c for c in result["checks"] if c["name"].startswith("contradiction_")]
        for c in contradiction_checks:
            assert c["status"] == "PASS"

    def test_missing_report_skips_contradiction_check(self, closure_env, tmp_path):
        """When report file doesn't exist, contradiction check is skipped (not failed)."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": "/nonexistent/path/report.md",
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        # Contradiction check should not appear since report doesn't exist
        contradiction_checks = [c for c in result["checks"] if c["name"].startswith("contradiction_")]
        assert len(contradiction_checks) == 0


# ---------------------------------------------------------------------------
# Multi-feature stale queue state test
# ---------------------------------------------------------------------------


class TestMultiFeatureStaleState:
    def test_stale_queue_during_active_dispatch_caught(self, closure_env):
        """Queue state from previous run is stale — per-PR closure catches it via reconciliation."""
        # PR-0 completed, PR-1 active
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_dispatch(
            closure_env["dispatch_dir"] / "active" / "d1.md", "PR-1", "d1"
        )

        # Try to close PR-1 — it's active, not completed
        result = cv.verify_pr_closure(
            pr_id="PR-1",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
        )

        assert result["verdict"] == "fail"
        assert result["reconciled_state"]["state"] == "active"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "pr_completed" in failed


# ---------------------------------------------------------------------------
# Round-3 codex findings (PR #300): pr_id guard, branch threading, CLI wiring,
# OPEN/CLEAN merge state in per-PR mode, contradiction detection in
# verify_closure
# ---------------------------------------------------------------------------


def _passing_gate_payload(pr_id: str, gate: str, report_path: Path, branch=None):
    payload = {
        "gate": gate,
        "pr_id": pr_id,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "contract_hash": "abcdef1234567890",
        "content_hash": "abcdef1234567890",
        "report_path": str(report_path),
    }
    if branch:
        payload["branch"] = branch
    return payload


class TestRound3PrIdGuard:
    """Finding 2: per-PR closure must reject a contract that names a different PR."""

    def test_contract_for_different_pr_id_rejected(self, closure_env, tmp_path):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        # Contract is for PR-1 but we are closing PR-0
        contract = _make_contract(pr_id="PR-1")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-1",
                           _passing_gate_payload("PR-1", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-1",
                           _passing_gate_payload("PR-1", "codex_gate", codex_report))

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "review_contract_pr_match" in failed
        # Mismatched contract must not contribute its (PR-1) gate evidence
        assert "gate_codex_gate" not in {
            c["name"] for c in result["checks"] if c["status"] == "PASS"
        }

    def test_matching_contract_pr_id_proceeds_normally(self, closure_env, tmp_path):
        """Sanity: contract.pr_id == pr_id still validates evidence."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report))

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        assert result["verdict"] == "pass"
        check_names = {c["name"] for c in result["checks"]}
        assert "review_contract_pr_match" not in check_names


class TestRound3BranchThreading:
    """Finding 3: gate results from a different branch must be rejected as stale."""

    def test_per_pr_closure_rejects_stale_branch_evidence(self, closure_env, tmp_path):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        # Gate results stamped with a *different* branch — older evidence
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report,
                                                 branch="feat/old-branch"))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report,
                                                 branch="feat/old-branch"))

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current-branch",
        )

        assert result["verdict"] == "fail"
        # Both gates' evidence must be reported missing because it is
        # tagged with a different branch.
        gate_failures = [
            c for c in result["checks"]
            if c["status"] == "FAIL" and c["name"].startswith("gate_")
        ]
        assert any("codex_gate" in c["name"] for c in gate_failures), gate_failures
        assert any("gemini_review" in c["name"] for c in gate_failures), gate_failures

    def test_per_pr_closure_accepts_matching_branch_evidence(self, closure_env, tmp_path):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report,
                                                 branch="feat/current"))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report,
                                                 branch="feat/current"))

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current",
        )

        assert result["verdict"] == "pass"

    def test_contradiction_detector_skips_stale_branch_evidence(self, closure_env, tmp_path):
        """A passing gate JSON paired with a [BLOCKING] report on a *different*
        branch must not trip the contradiction detector for the current branch
        — _detect_gate_report_contradictions must filter by branch first."""
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text(
            "# Gemini Review\n\n## Findings\n- [BLOCKING] stale finding from old branch\n"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        # Stale: report+gate on different branch — must be ignored entirely
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            **_passing_gate_payload("PR-0", "gemini_review", gemini_report,
                                    branch="feat/old"),
        })

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current",
        )

        # Stale evidence is ignored, so no contradiction check should fire
        contradiction_checks = [
            c for c in result["checks"] if c["name"].startswith("contradiction_")
        ]
        assert len(contradiction_checks) == 0, contradiction_checks


class TestRound3MainCliWiring:
    """Finding 3 + advisory: main() must forward --branch and --require-github-pr
    to verify_pr_closure in --pr-id mode."""

    def test_main_forwards_branch_and_require_github_pr(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(contract.to_json(indent=2))

        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report))

        captured = {}

        def fake_verify(**kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "mode": "per_pr", "pr_id": kwargs["pr_id"], "checks": []}

        monkeypatch.setattr(cv, "verify_pr_closure", fake_verify)

        rc = cv.main([
            "--pr-id", "PR-0",
            "--feature-plan", str(closure_env["feature_plan"]),
            "--review-contract", str(contract_path),
            "--gate-results-dir", str(results_dir),
            "--branch", "feat/current",
            "--mode", "pre_merge",
            "--require-github-pr",
            "--json",
        ])
        assert rc == 0
        assert captured["branch"] == "feat/current"
        assert captured["require_github_pr"] is True

    def test_require_github_pr_only_active_in_pre_merge_mode(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        contract = _make_contract(pr_id="PR-0")
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(contract.to_json(indent=2))

        captured = {}

        def fake_verify(**kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "mode": "per_pr", "pr_id": kwargs["pr_id"], "checks": []}

        monkeypatch.setattr(cv, "verify_pr_closure", fake_verify)

        rc = cv.main([
            "--pr-id", "PR-0",
            "--feature-plan", str(closure_env["feature_plan"]),
            "--review-contract", str(contract_path),
            "--branch", "feat/current",
            "--mode", "post_merge",
            "--require-github-pr",
            "--json",
        ])
        assert rc == 0
        # post_merge mode must downgrade --require-github-pr to False
        assert captured["require_github_pr"] is False

    def test_main_infers_branch_when_not_supplied(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        contract = _make_contract(pr_id="PR-0")
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(contract.to_json(indent=2))

        captured = {}

        def fake_verify(**kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "mode": "per_pr", "pr_id": kwargs["pr_id"], "checks": []}

        # Force the inferred branch lookup so the test does not depend on
        # the worktree's actual current branch.
        def fake_run(cmd, cwd=None, timeout=20):
            class _R:
                returncode = 0
                stdout = "feat/inferred\n"
                stderr = ""
            return _R()

        monkeypatch.setattr(cv, "verify_pr_closure", fake_verify)
        monkeypatch.setattr(cv, "_run", fake_run)

        rc = cv.main([
            "--pr-id", "PR-0",
            "--feature-plan", str(closure_env["feature_plan"]),
            "--review-contract", str(contract_path),
            "--mode", "pre_merge",
            "--require-github-pr",
            "--json",
        ])
        assert rc == 0
        assert captured["branch"] == "feat/inferred"
        assert captured["require_github_pr"] is True


class TestRound3MergeStateCheck:
    """Advisory finding: per-PR require_github_pr=True must enforce
    state == OPEN and mergeStateStatus == CLEAN, not just PR existence + green CI."""

    def test_blocked_pr_with_green_ci_fails_merge_state(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report))

        # PR exists, CI is green, but PR is BLOCKED (e.g. failing required review)
        def fake_find_branch_pr(branch):
            return {
                "number": 300,
                "url": "https://github.com/example/repo/pull/300",
                "state": "OPEN",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                ],
            }

        monkeypatch.setattr(cv, "_find_branch_pr", fake_find_branch_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current",
            require_github_pr=True,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "github_pr_mergeable" in failed

    def test_merged_pr_fails_merge_state_in_pre_merge_per_pr(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report))

        def fake_find_branch_pr(branch):
            return {
                "number": 300,
                "state": "MERGED",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                ],
            }

        monkeypatch.setattr(cv, "_find_branch_pr", fake_find_branch_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current",
            require_github_pr=True,
        )

        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "github_pr_mergeable" in failed

    def test_open_clean_pr_with_green_ci_passes(
        self, closure_env, tmp_path, monkeypatch
    ):
        _write_dispatch(
            closure_env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0"
        )
        _write_receipt(closure_env["receipts_file"], "d0")

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")

        contract = _make_contract(pr_id="PR-0")
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))
        _write_gate_result(results_dir, "codex_gate", "PR-0",
                           _passing_gate_payload("PR-0", "codex_gate", codex_report))

        def fake_find_branch_pr(branch):
            return {
                "number": 300,
                "state": "OPEN",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                ],
            }

        monkeypatch.setattr(cv, "_find_branch_pr", fake_find_branch_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=closure_env["project_root"],
            feature_plan=closure_env["feature_plan"],
            dispatch_dir=closure_env["dispatch_dir"],
            receipts_file=closure_env["receipts_file"],
            state_dir=closure_env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
            branch="feat/current",
            require_github_pr=True,
        )

        assert result["verdict"] == "pass"


class TestRound3VerifyClosureContradiction:
    """verify_closure (full-feature path) must call _detect_gate_report_contradictions."""

    def test_verify_closure_catches_pass_gate_with_blocking_report(
        self, closure_env, tmp_path, monkeypatch
    ):
        # Set up a fully-passing feature plan + queue + branch for verify_closure
        feature_plan = closure_env["project_root"] / "FEATURE_PLAN.md"
        feature_plan.write_text(
            "# Feature: R3 Closure Test\n\n"
            "**Status**: Complete\n\n"
            "## Dependency Flow\n```text\nPR-0\n```\n\n"
            "## PR-0: Foundation\n"
            "**Track**: A\n**Priority**: P1\n**Skill**: @backend-developer\n"
            "**Risk-Class**: high\n**Merge-Policy**: human\n"
            "**Review-Stack**: gemini_review\n**Dependencies**: []\n\n"
            "`gate_pr0`\n\n---\n"
        )
        pr_queue = closure_env["project_root"] / "PR_QUEUE.md"
        pr_queue.write_text(
            "# PR Queue - Feature: R3 Closure Test\n\n"
            "Total: 1 PRs | Complete: 1 | Active: 0 | Queued: 0 | Blocked: 0\n\n"
            "## Dependency Flow\n```\nPR-0\n```\n"
        )

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        gemini_report = reports_dir / "gemini.md"
        # Report contradicts the gate JSON: gate says pass, report says blocking
        gemini_report.write_text(
            "# Gemini Review\n\n## Findings\n"
            "- [BLOCKING] mismatch in queue scanner\n"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        results_dir = tmp_path / "results"
        _write_gate_result(results_dir, "gemini_review", "PR-0",
                           _passing_gate_payload("PR-0", "gemini_review", gemini_report))

        # Stub external GitHub probes so we focus on contradiction detection
        monkeypatch.setattr(cv, "_remote_branch_exists", lambda b, root: True)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda b: {
            "number": 300,
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},
            ],
        })

        result = cv.verify_closure(
            project_root=closure_env["project_root"],
            feature_plan=feature_plan,
            pr_queue=pr_queue,
            branch="feat/current",
            mode="pre_merge",
            review_contract=contract,
            gate_results_dir=results_dir,
        )

        check_names = {c["name"] for c in result["checks"]}
        assert "contradiction_gemini_review" in check_names
        contradiction_check = next(
            c for c in result["checks"] if c["name"] == "contradiction_gemini_review"
        )
        assert contradiction_check["status"] == "FAIL"
        assert result["verdict"] == "fail"
