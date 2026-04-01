#!/usr/bin/env python3
"""Gate evidence enforcement tests — REMEDIATION-1 quality gate.

Gate: remediation_gate_evidence_enforcement

Covers:
  1. record_result rejects non-existent report_path for pass/fail
  2. record_result accepts existing report_path for pass/fail
  3. _find_gate_result requires pr_id match (AND, not OR)
  4. _find_gate_result rejects results from a different branch
  5. closure_verifier checks verdict field (Codex) for report_path enforcement
  6. record_result allows queued/requested status without report_path
"""

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import review_gate_manager as rgm
import closure_verifier as cv


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def review_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


# ---------------------------------------------------------------------------
# Test 1: record_result rejects non-existent report_path for pass/fail
# ---------------------------------------------------------------------------

def test_record_result_rejects_missing_report_file(review_env, monkeypatch):
    """record_result with status=pass and a non-existent report_path raises ValueError."""
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    nonexistent = str(review_env / ".vnx-data" / "unified_reports" / "does-not-exist.md")

    with pytest.raises(ValueError, match="report_path file does not exist"):
        manager.record_result(
            gate="gemini_review",
            pr_number=30,
            branch="feature/test",
            status="pass",
            summary="No blocking findings",
            contract_hash="hash-abc",
            report_path=nonexistent,
        )


# ---------------------------------------------------------------------------
# Test 2: record_result accepts existing report_path for pass/fail
# ---------------------------------------------------------------------------

def test_record_result_accepts_existing_report_file(review_env, monkeypatch):
    """record_result with status=pass and an existing report_path succeeds."""
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    report_file = review_env / ".vnx-data" / "unified_reports" / "real-report.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# Gate report\n", encoding="utf-8")

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=31,
        branch="feature/test",
        status="pass",
        summary="No blocking findings",
        contract_hash="hash-def",
        report_path=str(report_file),
    )

    assert payload["status"] == "pass"
    assert payload["report_path"] == str(report_file.resolve())


# ---------------------------------------------------------------------------
# Test 3: _find_gate_result requires pr_id match (AND, not OR)
# ---------------------------------------------------------------------------

def test_find_gate_result_requires_pr_id_match(tmp_path):
    """A result from a different PR is not returned even if gate name matches."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    # Write a result for PR-99 with gate=gemini_review
    result_data = {
        "pr_id": "PR-99",
        "gate": "gemini_review",
        "status": "pass",
        "report_path": "",
    }
    result_file = results_dir / "pr-99-gemini_review.json"
    result_file.write_text(json.dumps(result_data), encoding="utf-8")

    # Query for PR-1 — must not return the PR-99 result
    found = cv._find_gate_result("gemini_review", "PR-1", results_dir)
    assert found is None, (
        "_find_gate_result must require pr_id match; OR logic allows stale PR results to satisfy closure"
    )


# ---------------------------------------------------------------------------
# Test 4: _find_gate_result rejects results from a different branch
# ---------------------------------------------------------------------------

def test_find_gate_result_rejects_stale_branch(tmp_path):
    """A result from a different branch is not returned when branch is provided."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    result_data = {
        "pr_id": "PR-2",
        "gate": "codex_gate",
        "status": "pass",
        "branch": "feature/old-feature",
        "report_path": "",
    }
    (results_dir / "pr-2-codex_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8"
    )

    # Query with the current feature branch — must reject the old-feature result
    found = cv._find_gate_result(
        "codex_gate", "PR-2", results_dir, branch="feature/current-feature"
    )
    assert found is None, (
        "_find_gate_result must reject results from a different branch to prevent "
        "stale prior-feature gate results satisfying current-feature closure"
    )


# ---------------------------------------------------------------------------
# Test 5: closure_verifier checks verdict field for report_path enforcement
# ---------------------------------------------------------------------------

def test_closure_checks_verdict_field(tmp_path):
    """A gate result using 'verdict' (Codex) instead of 'status' triggers report_path check."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    # Codex result uses 'verdict' not 'status', and points to a non-existent report
    result_data = {
        "pr_id": "PR-3",
        "gate": "codex_gate",
        "verdict": "pass",
        "report_path": str(tmp_path / "nonexistent-codex-report.md"),
    }
    (results_dir / "pr-3-codex_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8"
    )

    from review_contract import ReviewContract

    contract = ReviewContract(
        pr_id="PR-3",
        review_stack=["codex_gate"],
        content_hash="hash-xyz",
    )

    checks = cv._validate_review_evidence(contract, results_dir)

    report_checks = [c for c in checks if c.name.startswith("report_")]
    assert report_checks, "Expected at least one report_ check result"
    failing = [c for c in report_checks if c.status == "FAIL"]
    assert failing, (
        "Expected report_path FAIL for verdict=pass result with non-existent file; "
        "closure must enforce report existence for Codex verdict-only results"
    )


# ---------------------------------------------------------------------------
# Test 6: record_result allows queued/requested status without report_path
# ---------------------------------------------------------------------------

def test_record_result_allows_queued_without_report(review_env, monkeypatch):
    """queued and requested statuses do not require contract_hash or report_path."""
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    for status in ("queued", "requested"):
        payload = manager.record_result(
            gate="gemini_review",
            pr_number=40,
            branch="feature/test",
            status=status,
            summary="Review queued",
        )
        assert payload["status"] == status, f"Expected status={status} in payload"
