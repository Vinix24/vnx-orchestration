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
