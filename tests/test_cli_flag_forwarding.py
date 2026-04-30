#!/usr/bin/env python3
"""End-to-end CLI flag forwarding tests for closure_verifier.py and review_gate_manager.py.

Background (from claudedocs/2026-04-29-codex-findings-synthesis.md §4.2):
PRs #300 and #301 shipped two distinct flag-forwarding gaps in two consecutive
PRs (--branch, --mode, --require-github-pr parsed by argparse but never propagated
into the verifier function body). This test exercises every supported CLI flag
of both scripts and asserts the inner function receives it. It is the regression
backstop for the recurring "argparse parses, body ignores" anti-pattern.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv  # noqa: E402
import review_gate_manager as rgm  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Minimal VNX env so ensure_env() resolves cleanly inside main()."""
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    dispatch_dir = data_dir / "dispatches"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = data_dir / "unified_reports"
    (reports_dir / "headless").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(reports_dir / "headless"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


def _stub_verify_pr_closure_return() -> dict:
    return {"verdict": "pass", "checks": []}


def _stub_verify_closure_return() -> dict:
    return {"verdict": "pass", "checks": [], "feature_title": "demo"}


# ---------------------------------------------------------------------------
# closure_verifier.py — per-PR mode (--pr-id)
# ---------------------------------------------------------------------------


def test_per_pr_forwards_pr_id_and_branch(project_env):
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        rc = cv.main(["--pr-id", "PR-7", "--branch", "feat/x"])
    assert rc == 0
    kwargs = mock.call_args.kwargs
    assert kwargs["pr_id"] == "PR-7"
    assert kwargs["branch"] == "feat/x"


def test_per_pr_pre_merge_auto_requires_github_pr(project_env):
    """--mode pre_merge in --pr-id mode auto-enables require_github_pr (CFX-2 fix)."""
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        cv.main(["--pr-id", "PR-7", "--branch", "feat/x", "--mode", "pre_merge"])
    assert mock.call_args.kwargs["require_github_pr"] is True


def test_per_pr_post_merge_does_not_auto_require_github_pr(project_env):
    """post_merge mode must not auto-enable require_github_pr — caller may opt in explicitly."""
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        cv.main(["--pr-id", "PR-7", "--branch", "feat/x", "--mode", "post_merge"])
    assert mock.call_args.kwargs["require_github_pr"] is False


def test_per_pr_explicit_require_github_pr_flag(project_env):
    """--require-github-pr forwards even when --mode is post_merge."""
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        cv.main([
            "--pr-id", "PR-7",
            "--branch", "feat/x",
            "--mode", "post_merge",
            "--require-github-pr",
        ])
    assert mock.call_args.kwargs["require_github_pr"] is True


def test_per_pr_review_contract_loaded_and_forwarded(project_env, tmp_path):
    """--review-contract path is parsed into a ReviewContract and forwarded."""
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps({
        "pr_id": "PR-7",
        "review_stack": ["codex_gate"],
        "risk_class": "medium",
        "deterministic_findings": [],
        "deliverables": [],
        "quality_gates": [],
        "test_evidence": [],
        "content_hash": "abc",
    }))
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        cv.main([
            "--pr-id", "PR-7",
            "--branch", "feat/x",
            "--review-contract", str(contract_path),
        ])
    forwarded = mock.call_args.kwargs["review_contract"]
    assert forwarded is not None
    assert forwarded.pr_id == "PR-7"


def test_per_pr_gate_results_dir_forwarded_as_path(project_env, tmp_path):
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()
    with patch.object(cv, "verify_pr_closure", return_value=_stub_verify_pr_closure_return()) as mock:
        cv.main([
            "--pr-id", "PR-7",
            "--branch", "feat/x",
            "--gate-results-dir", str(gate_dir),
        ])
    assert mock.call_args.kwargs["gate_results_dir"] == gate_dir


# ---------------------------------------------------------------------------
# closure_verifier.py — whole-feature mode (no --pr-id)
# ---------------------------------------------------------------------------


def test_full_mode_forwards_branch_and_mode(project_env):
    """Whole-feature path: --branch and --mode reach verify_closure()."""
    with patch.object(cv, "verify_closure", return_value=_stub_verify_closure_return()) as mock:
        cv.main(["--branch", "feat/x", "--mode", "post_merge"])
    kwargs = mock.call_args.kwargs
    assert kwargs["branch"] == "feat/x"
    assert kwargs["mode"] == "post_merge"


def test_full_mode_forwards_review_contract(project_env, tmp_path):
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps({
        "pr_id": "PR-9",
        "review_stack": ["gemini_review"],
        "risk_class": "low",
        "deterministic_findings": [],
        "deliverables": [],
        "quality_gates": [],
        "test_evidence": [],
        "content_hash": "h",
    }))
    with patch.object(cv, "verify_closure", return_value=_stub_verify_closure_return()) as mock:
        cv.main([
            "--branch", "feat/x",
            "--mode", "pre_merge",
            "--review-contract", str(contract_path),
        ])
    forwarded = mock.call_args.kwargs["review_contract"]
    assert forwarded is not None
    assert forwarded.pr_id == "PR-9"


def test_full_mode_forwards_gate_results_dir(project_env, tmp_path):
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()
    with patch.object(cv, "verify_closure", return_value=_stub_verify_closure_return()) as mock:
        cv.main(["--branch", "feat/x", "--gate-results-dir", str(gate_dir)])
    assert mock.call_args.kwargs["gate_results_dir"] == gate_dir


def test_full_mode_forwards_claim_file_when_exists(project_env, tmp_path):
    claim_file = tmp_path / "claim.json"
    claim_file.write_text("{}")
    with patch.object(cv, "verify_closure", return_value=_stub_verify_closure_return()) as mock:
        cv.main(["--branch", "feat/x", "--claim-file", str(claim_file)])
    assert mock.call_args.kwargs["claim_file"] == claim_file


def test_full_mode_skips_claim_file_when_missing(project_env, tmp_path):
    """Non-existent claim file should be passed as None (preserves prior behavior)."""
    missing = tmp_path / "no_such.json"
    with patch.object(cv, "verify_closure", return_value=_stub_verify_closure_return()) as mock:
        cv.main(["--branch", "feat/x", "--claim-file", str(missing)])
    assert mock.call_args.kwargs["claim_file"] is None


def test_verify_closure_invokes_contradiction_detector(project_env, tmp_path):
    """verify_closure() must call _detect_gate_report_contradictions when a contract is present (CFX-2 fix)."""
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps({
        "pr_id": "PR-9",
        "review_stack": ["codex_gate"],
        "risk_class": "medium",
        "deterministic_findings": [],
        "deliverables": [],
        "quality_gates": [],
        "test_evidence": [],
        "content_hash": "h",
    }))
    # Patch the helpers verify_closure depends on so we can run it without git.
    with patch.object(cv, "_detect_gate_report_contradictions", return_value=[]) as detector, \
         patch.object(cv, "_validate_review_evidence", return_value=[]), \
         patch.object(cv, "_check_stale_staging", return_value=cv.CheckResult("stale_staging", "PASS", "ok")), \
         patch.object(cv, "_validate_test_claims", return_value=[]), \
         patch.object(cv, "_remote_branch_exists", return_value=True), \
         patch.object(cv, "_find_branch_pr", return_value=None), \
         patch.object(cv, "_parse_feature_plan", return_value={
             "title": "f", "status": "complete", "dependency_flow": "x", "pr_ids": ["PR-9"]
         }), \
         patch.object(cv, "_parse_pr_queue", return_value={
             "title": "f", "overview": (1, 1, 0, 0, 0), "dependency_flow": "x"
         }):
        from review_contract import ReviewContract
        contract = ReviewContract.from_json(contract_path.read_text())
        cv.verify_closure(
            project_root=project_env,
            feature_plan=project_env / "FEATURE_PLAN.md",
            pr_queue=project_env / "PR_QUEUE.md",
            branch="feat/x",
            mode="pre_merge",
            review_contract=contract,
        )
    assert detector.called, "verify_closure() must wire _detect_gate_report_contradictions into its checks"


# ---------------------------------------------------------------------------
# review_gate_manager.py — every subcommand × every flag
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace ReviewGateManager() and the git-diff fallback so main() runs offline."""
    fake = MagicMock()
    fake.request_reviews.return_value = {"requested": []}
    fake.request_and_execute.return_value = {"has_required_failure": False, "gates": []}
    fake.execute_gate.return_value = {"gate": "x"}
    fake.record_result.return_value = {"recorded": True}
    fake.status.return_value = {}
    monkeypatch.setattr(rgm, "ReviewGateManager", lambda: fake)
    monkeypatch.setattr(rgm, "_compute_changed_files", lambda branch: ["a.py", "b.py"])
    return fake


def test_rgm_request_forwards_all_flags(fake_manager):
    rc = rgm.main([
        "request",
        "--pr", "42",
        "--branch", "feat/x",
        "--review-stack", "codex_gate,gemini_review",
        "--risk-class", "high",
        "--changed-files", "a.py,b.py",
        "--mode", "final",
        "--dispatch-id", "D-1",
    ])
    assert rc == 0
    kwargs = fake_manager.request_reviews.call_args.kwargs
    assert kwargs["pr_number"] == 42
    assert kwargs["branch"] == "feat/x"
    assert kwargs["review_stack"] == ["codex_gate", "gemini_review"]
    assert kwargs["risk_class"] == "high"
    assert kwargs["changed_files"] == ["a.py", "b.py"]
    assert kwargs["mode"] == "final"
    assert kwargs["dispatch_id"] == "D-1"


def test_rgm_request_auto_computes_changed_files_when_blank(fake_manager):
    """Empty --changed-files triggers _compute_changed_files fallback."""
    rgm.main([
        "request",
        "--pr", "5",
        "--branch", "feat/x",
        "--changed-files", "",
    ])
    assert fake_manager.request_reviews.call_args.kwargs["changed_files"] == ["a.py", "b.py"]


def test_rgm_request_and_execute_forwards_all_flags(fake_manager):
    rc = rgm.main([
        "request-and-execute",
        "--pr", "42",
        "--branch", "feat/x",
        "--review-stack", "codex_gate",
        "--risk-class", "low",
        "--changed-files", "a.py",
        "--mode", "per_pr",
        "--dispatch-id", "D-2",
    ])
    assert rc == 0
    kwargs = fake_manager.request_and_execute.call_args.kwargs
    assert kwargs["pr_number"] == 42
    assert kwargs["branch"] == "feat/x"
    assert kwargs["review_stack"] == ["codex_gate"]
    assert kwargs["risk_class"] == "low"
    assert kwargs["changed_files"] == ["a.py"]
    assert kwargs["mode"] == "per_pr"
    assert kwargs["dispatch_id"] == "D-2"


def test_rgm_execute_forwards_gate_pr_pr_id(fake_manager):
    rc = rgm.main([
        "execute",
        "--gate", "codex_gate",
        "--pr", "42",
        "--pr-id", "PR-7",
    ])
    assert rc == 0
    kwargs = fake_manager.execute_gate.call_args.kwargs
    assert kwargs["gate"] == "codex_gate"
    assert kwargs["pr_number"] == 42
    assert kwargs["pr_id"] == "PR-7"


def test_rgm_record_result_forwards_all_flags(fake_manager, tmp_path):
    findings_file = tmp_path / "findings.json"
    findings_file.write_text(json.dumps([{"severity": "error", "message": "x"}]))
    rc = rgm.main([
        "record-result",
        "--gate", "codex_gate",
        "--pr", "42",
        "--branch", "feat/x",
        "--status", "pass",
        "--summary", "ok",
        "--findings-file", str(findings_file),
        "--residual-risk", "low",
        "--contract-hash", "abc123",
        "--pr-id", "PR-7",
        "--report-path", "/tmp/r.md",
    ])
    assert rc == 0
    kwargs = fake_manager.record_result.call_args.kwargs
    assert kwargs["gate"] == "codex_gate"
    assert kwargs["pr_number"] == 42
    assert kwargs["branch"] == "feat/x"
    assert kwargs["status"] == "pass"
    assert kwargs["summary"] == "ok"
    assert kwargs["findings"] == [{"severity": "error", "message": "x"}]
    assert kwargs["residual_risk"] == "low"
    assert kwargs["contract_hash"] == "abc123"
    assert kwargs["pr_id"] == "PR-7"
    assert kwargs["report_path"] == "/tmp/r.md"


def test_rgm_status_forwards_pr(fake_manager):
    rc = rgm.main(["status", "--pr", "42"])
    assert rc == 0
    fake_manager.status.assert_called_once_with(42)
