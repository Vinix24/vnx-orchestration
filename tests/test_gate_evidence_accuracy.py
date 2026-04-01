#!/usr/bin/env python3
"""Gate evidence accuracy tests — gate_pr1_gate_evidence_accuracy.

Covers four deterministic fixes:

  Fix 1 — Deterministic provenance:
      scan_dispatch_dirs() uses sorted(iterdir()) so multiple dispatches per PR
      always yield the same ordering regardless of filesystem iteration order.

  Fix 2 — PR-scoped AND gate lookup (contract path):
      _find_gate_result() rejects a contract file whose JSON pr_id does not match
      the queried pr_id, even when the filename-based path resolves to a file.
      This closes the cross-PR attribution gap left in the legacy-glob AND check.

  Fix 3 — Verdict-only report_path escape:
      A gate result with status="requested" (truthy) AND verdict="pass" must still
      trigger report_path enforcement. The old OR-priority logic used status first
      and would skip enforcement for this combination.

  Fix 4 — GitHub PR + CI required for per-PR merge closure:
      verify_pr_closure(require_github_pr=True) fails when no GitHub PR exists for
      the branch, and fails when GitHub CI checks are not green.
      Local-only closure cannot pass merge readiness without real GitHub evidence.

Scenario coverage: success (clean gate result), cross-PR miss (Fix 2),
                   verdict escape (Fix 3), no-github-pr (Fix 4),
                   failing-github-checks (Fix 4), green-github-checks (Fix 4),
                   deterministic-order (Fix 1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
# Shared helpers
# ---------------------------------------------------------------------------

FEATURE_PLAN_CONTENT = """\
# Feature: Gate Evidence Accuracy Test Feature

**Status**: Active
**Risk-Class**: high

## PR-0: Foundation
**Track**: C
**Priority**: P1
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: []

`gate_pr0_foundation`

---
"""


def _write_dispatch(path: Path, pr_id: str, dispatch_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[[TARGET:C]]\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n",
        encoding="utf-8",
    )


def _write_receipt(receipts_file: Path, dispatch_id: str) -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "dispatch_id": dispatch_id,
        "event_type": "task_complete",
        "status": "success",
    }
    with receipts_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _make_contract(
    pr_id: str = "PR-0",
    review_stack=None,
    risk_class: str = "high",
) -> ReviewContract:
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Test PR",
        feature_title="Gate Evidence Test",
        branch="feature/test",
        track="C",
        risk_class=risk_class,
        merge_policy="human",
        review_stack=review_stack or ["gemini_review", "codex_gate"],
        closure_stage="in_review",
        deliverables=[Deliverable(description="test", category="implementation")],
        non_goals=[],
        scope_files=[],
        changed_files=[],
        quality_gate=QualityGate(gate_id="gate_pr0_foundation", checks=["check 1"]),
        test_evidence=TestEvidence(
            test_files=["tests/test_gate_evidence_accuracy.py"],
            test_command="pytest tests/test_gate_evidence_accuracy.py",
        ),
        deterministic_findings=[],
        content_hash="test-hash-abc123",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Minimal VNX environment wired to tmp_path."""
    project_root = tmp_path / "repo"
    project_root.mkdir()

    data_dir = project_root / ".vnx-data"
    dispatch_dir = data_dir / "dispatches"
    dispatch_dir.mkdir(parents=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)

    (project_root / "FEATURE_PLAN.md").write_text(
        FEATURE_PLAN_CONTENT, encoding="utf-8"
    )

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
        "data_dir": data_dir,
        "dispatch_dir": dispatch_dir,
        "state_dir": state_dir,
        "receipts": state_dir / "t0_receipts.ndjson",
        "feature_plan": project_root / "FEATURE_PLAN.md",
    }


# ---------------------------------------------------------------------------
# Fix 1: Deterministic provenance ordering
# ---------------------------------------------------------------------------

class TestDeterministicProvenance:
    """
    scan_dispatch_dirs() must return records in a stable, sorted order when
    multiple dispatch files exist in the same directory.
    """

    def test_ordering_is_stable_across_calls(self, tmp_path):
        """Two calls to scan_dispatch_dirs return the same dispatch ordering."""
        dispatch_dir = tmp_path / "dispatches"
        completed_dir = dispatch_dir / "completed"
        completed_dir.mkdir(parents=True)

        # Create several files whose os.listdir() order is typically arbitrary
        names = [
            "20260401-120000-dispatch-z-B.md",
            "20260401-110000-dispatch-a-B.md",
            "20260401-115000-dispatch-m-B.md",
        ]
        for name in names:
            f = completed_dir / name
            f.write_text(
                f"PR-ID: PR-1\nDispatch-ID: {f.stem}\n", encoding="utf-8"
            )

        records1 = scan_dispatch_dirs(dispatch_dir)
        records2 = scan_dispatch_dirs(dispatch_dir)

        ids1 = [r.dispatch_id for r in records1]
        ids2 = [r.dispatch_id for r in records2]

        assert ids1 == ids2, "scan_dispatch_dirs must return stable ordering"

    def test_ordering_matches_lexicographic_sort(self, tmp_path):
        """Files are returned in lexicographic (sorted) order by filename stem."""
        dispatch_dir = tmp_path / "dispatches"
        completed_dir = dispatch_dir / "completed"
        completed_dir.mkdir(parents=True)

        stems = [
            "20260401-120000-zzz-B",
            "20260401-100000-aaa-B",
            "20260401-110000-mmm-B",
        ]
        for stem in stems:
            (completed_dir / f"{stem}.md").write_text(
                f"PR-ID: PR-1\nDispatch-ID: {stem}\n", encoding="utf-8"
            )

        records = scan_dispatch_dirs(dispatch_dir)
        ids = [r.dispatch_id for r in records]
        assert ids == sorted(ids), (
            "Dispatch records must be returned in sorted filename order"
        )

    def test_multiple_dispatches_per_pr_deterministic(self, tmp_path):
        """Multiple dispatches for the same PR are collected in stable sorted order."""
        dispatch_dir = tmp_path / "dispatches"
        for sub in ("completed", "active"):
            (dispatch_dir / sub).mkdir(parents=True)

        # Two completed dispatches for PR-1 — the second (alphabetically) must come last
        (dispatch_dir / "completed" / "dispatch-001-PR1-early.md").write_text(
            "PR-ID: PR-1\nDispatch-ID: dispatch-001-PR1-early\n", encoding="utf-8"
        )
        (dispatch_dir / "completed" / "dispatch-002-PR1-late.md").write_text(
            "PR-ID: PR-1\nDispatch-ID: dispatch-002-PR1-late\n", encoding="utf-8"
        )

        records = scan_dispatch_dirs(dispatch_dir)
        pr1_records = [r for r in records if r.pr_id == "PR-1"]
        assert len(pr1_records) == 2
        assert pr1_records[0].dispatch_id == "dispatch-001-PR1-early"
        assert pr1_records[1].dispatch_id == "dispatch-002-PR1-late"


# ---------------------------------------------------------------------------
# Fix 2: PR-scoped AND gate lookup — contract path
# ---------------------------------------------------------------------------

class TestPrScopedGateLookup:
    """
    _find_gate_result() must reject a contract file whose JSON pr_id does not
    match the queried pr_id, even when the filename resolves to a contract for
    a different PR (cross-PR attribution gap).
    """

    def test_contract_path_rejects_wrong_pr_in_json(self, tmp_path):
        """
        File pr1-gemini_review-contract.json contains pr_id='PR-2'.
        Query for PR-1 must return None — the JSON pr_id is authoritative.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        # Filename matches PR-1 lookup, but JSON contains PR-2 data
        wrong_pr_data = {
            "pr_id": "PR-2",
            "gate": "gemini_review",
            "status": "pass",
            "report_path": "",
        }
        (results_dir / "pr1-gemini_review-contract.json").write_text(
            json.dumps(wrong_pr_data), encoding="utf-8"
        )

        found = cv._find_gate_result("gemini_review", "PR-1", results_dir)
        assert found is None, (
            "_find_gate_result must reject contract path when JSON pr_id='PR-2' "
            "does not match queried pr_id='PR-1' — cross-PR attribution must be blocked"
        )

    def test_contract_path_accepts_correct_pr_in_json(self, tmp_path):
        """
        File pr1-gemini_review-contract.json contains pr_id='PR-1'.
        Query for PR-1 must return the data.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        correct_pr_data = {
            "pr_id": "PR-1",
            "gate": "gemini_review",
            "status": "pass",
            "report_path": "",
        }
        (results_dir / "pr1-gemini_review-contract.json").write_text(
            json.dumps(correct_pr_data), encoding="utf-8"
        )

        found = cv._find_gate_result("gemini_review", "PR-1", results_dir)
        assert found is not None
        assert found["pr_id"] == "PR-1"

    def test_contract_path_accepts_result_without_pr_id_field(self, tmp_path):
        """
        Contract file has no pr_id field at all (legacy format).
        Must still be returned — absence of pr_id in JSON is not a rejection.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        no_pr_id_data = {
            "gate": "gemini_review",
            "status": "pass",
            "report_path": "",
        }
        (results_dir / "pr1-gemini_review-contract.json").write_text(
            json.dumps(no_pr_id_data), encoding="utf-8"
        )

        found = cv._find_gate_result("gemini_review", "PR-1", results_dir)
        assert found is not None, (
            "Contract file without pr_id field must not be rejected — "
            "only a mismatching pr_id should be blocked"
        )

    def test_legacy_glob_still_requires_and_match(self, tmp_path):
        """Legacy glob path continues to require pr_id AND gate match."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        wrong_pr = {
            "pr_id": "PR-99",
            "gate": "codex_gate",
            "status": "pass",
            "report_path": "",
        }
        (results_dir / "pr-99-codex_gate.json").write_text(
            json.dumps(wrong_pr), encoding="utf-8"
        )

        found = cv._find_gate_result("codex_gate", "PR-1", results_dir)
        assert found is None, "Legacy glob path must not return results from PR-99 for PR-1 query"

    def test_no_cross_pr_attribution_with_both_paths(self, tmp_path):
        """
        PR-1 and PR-2 both have contract files. PR-1 query never returns PR-2 data,
        and vice versa.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        (results_dir / "pr1-gemini_review-contract.json").write_text(
            json.dumps({"pr_id": "PR-1", "gate": "gemini_review", "status": "pass"}),
            encoding="utf-8",
        )
        (results_dir / "pr2-gemini_review-contract.json").write_text(
            json.dumps({"pr_id": "PR-2", "gate": "gemini_review", "status": "pass"}),
            encoding="utf-8",
        )

        found_pr1 = cv._find_gate_result("gemini_review", "PR-1", results_dir)
        found_pr2 = cv._find_gate_result("gemini_review", "PR-2", results_dir)

        assert found_pr1 is not None and found_pr1["pr_id"] == "PR-1"
        assert found_pr2 is not None and found_pr2["pr_id"] == "PR-2"


# ---------------------------------------------------------------------------
# Fix 3: Verdict-only report_path escape
# ---------------------------------------------------------------------------

class TestVerdictOnlyReportPathEnforcement:
    """
    A gate result with status="requested" (truthy non-terminal) AND verdict="pass"
    must still trigger report_path enforcement.  The old OR-priority logic would
    resolve gate_status="requested" and skip the check.
    """

    def test_verdict_pass_with_nonterminal_status_enforces_report_path(self, tmp_path):
        """
        result = {status: "requested", verdict: "pass"} with no report_path
        must produce a FAIL on the report_ check.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        # status is non-terminal ("requested") but verdict is terminal ("pass")
        result_data = {
            "pr_id": "PR-0",
            "gate": "gemini_review",
            "status": "requested",
            "verdict": "pass",
            # No report_path
        }
        (results_dir / "pr0-gemini_review-contract.json").write_text(
            json.dumps(result_data), encoding="utf-8"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        checks = cv._validate_review_evidence(contract, results_dir)

        report_checks = [c for c in checks if c.name.startswith("report_")]
        assert report_checks, "Expected at least one report_ check"
        failing = [c for c in report_checks if c.status == "FAIL"]
        assert failing, (
            "report_path enforcement must trigger when verdict='pass' even when "
            "status='requested' — the OR-priority escape must be closed"
        )
        assert any("missing required report_path" in c.detail for c in failing)

    def test_verdict_only_no_status_field_enforces_report_path(self, tmp_path):
        """
        result = {verdict: "pass"} with no status field or report_path.
        Must produce FAIL — basic verdict-only case already working.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result_data = {
            "pr_id": "PR-0",
            "gate": "codex_gate",
            "verdict": "pass",
            # No status, no report_path
        }
        (results_dir / "pr0-codex_gate-contract.json").write_text(
            json.dumps(result_data), encoding="utf-8"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["codex_gate"])
        checks = cv._validate_review_evidence(contract, results_dir)

        report_checks = [c for c in checks if c.name.startswith("report_")]
        assert report_checks
        failing = [c for c in report_checks if c.status == "FAIL"]
        assert failing, "Verdict-only result without report_path must fail validation"

    def test_verdict_fail_with_nonterminal_status_enforces_report_path(self, tmp_path):
        """
        result = {status: "in_progress", verdict: "fail"} must also trigger enforcement.
        verdict="fail" is terminal and must not escape through status priority.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result_data = {
            "pr_id": "PR-0",
            "gate": "gemini_review",
            "status": "in_progress",
            "verdict": "fail",
        }
        (results_dir / "pr0-gemini_review-contract.json").write_text(
            json.dumps(result_data), encoding="utf-8"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        checks = cv._validate_review_evidence(contract, results_dir)

        report_checks = [c for c in checks if c.name.startswith("report_")]
        failing = [c for c in report_checks if c.status == "FAIL"]
        assert failing, (
            "verdict='fail' with non-terminal status must still enforce report_path"
        )

    def test_non_terminal_status_and_no_verdict_does_not_require_report_path(self, tmp_path):
        """
        result = {status: "queued"} with no verdict — neither field is terminal.
        report_path enforcement must NOT trigger (the result is mid-flight).
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        result_data = {
            "pr_id": "PR-0",
            "gate": "gemini_review",
            "status": "queued",
        }
        (results_dir / "pr0-gemini_review-contract.json").write_text(
            json.dumps(result_data), encoding="utf-8"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        checks = cv._validate_review_evidence(contract, results_dir)

        report_checks = [c for c in checks if c.name.startswith("report_")]
        # Non-terminal statuses must NOT produce report_ checks at all
        fail_checks = [c for c in report_checks if c.status == "FAIL" and "report_path" in c.detail]
        assert not fail_checks, (
            "Non-terminal status 'queued' with no verdict must not require report_path"
        )

    def test_pass_status_with_existing_report_passes(self, tmp_path):
        """
        result = {status: "pass", report_path: <existing file>} → PASS.
        Regression guard: correct results must continue to pass.
        """
        results_dir = tmp_path / "results"
        results_dir.mkdir()

        report_file = tmp_path / "reports" / "gate-report.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text("# Gate Report\nAll clear.\n", encoding="utf-8")

        result_data = {
            "pr_id": "PR-0",
            "gate": "gemini_review",
            "status": "pass",
            "report_path": str(report_file),
            "blocking_count": 0,
        }
        (results_dir / "pr0-gemini_review-contract.json").write_text(
            json.dumps(result_data), encoding="utf-8"
        )

        contract = _make_contract(pr_id="PR-0", review_stack=["gemini_review"])
        checks = cv._validate_review_evidence(contract, results_dir)

        report_checks = [c for c in checks if c.name.startswith("report_")]
        assert report_checks
        assert all(c.status == "PASS" for c in report_checks), (
            "Result with valid report_path must produce PASS report checks"
        )


# ---------------------------------------------------------------------------
# Fix 4: GitHub PR + CI required for per-PR merge closure
# ---------------------------------------------------------------------------

class TestGithubPrCiRequiredForClosure:
    """
    verify_pr_closure(require_github_pr=True) must fail when:
    - No GitHub PR exists for the branch
    - GitHub CI checks are not green (pending or failing)

    Local-only closure cannot pass merge readiness without real GitHub evidence.
    """

    def _setup_completed_pr(self, env: dict) -> None:
        """Write a completed dispatch + receipt so PR-0 reconciles as 'completed'."""
        dispatch_id = "dispatch-pr0-complete-001"
        _write_dispatch(
            env["dispatch_dir"] / "completed" / f"{dispatch_id}.md",
            pr_id="PR-0",
            dispatch_id=dispatch_id,
        )
        _write_receipt(env["receipts"], dispatch_id)

    def test_no_github_pr_blocks_merge_readiness(self, env, monkeypatch):
        """
        When branch has no GitHub PR, github_pr_exists=FAIL blocks the verdict.
        """
        self._setup_completed_pr(env)

        # Mock GitHub to return no PR
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: None)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/test-no-pr",
            require_github_pr=True,
        )

        github_check = next(
            (c for c in result["checks"] if c["name"] == "github_pr_exists"), None
        )
        assert github_check is not None, "github_pr_exists check must be present"
        assert github_check["status"] == "FAIL", (
            "Missing GitHub PR must block merge readiness"
        )
        assert result["verdict"] == "fail"

    def test_failing_github_checks_block_merge_readiness(self, env, monkeypatch):
        """
        GitHub PR exists but CI checks are not green → github_checks=FAIL.
        """
        self._setup_completed_pr(env)

        failing_pr = {
            "number": 42,
            "state": "OPEN",
            "url": "https://github.com/org/repo/pull/42",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "CI",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
        }
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: failing_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/test-failing-checks",
            require_github_pr=True,
        )

        ci_check = next(
            (c for c in result["checks"] if c["name"] == "github_checks"), None
        )
        assert ci_check is not None, "github_checks check must be present when PR exists"
        assert ci_check["status"] == "FAIL", (
            "Failing GitHub CI must block merge readiness"
        )
        assert result["verdict"] == "fail"

    def test_pending_github_checks_block_merge_readiness(self, env, monkeypatch):
        """
        GitHub PR exists but CI checks are IN_PROGRESS → github_checks=FAIL.
        Pending is not the same as passing — must not let closure through.
        """
        self._setup_completed_pr(env)

        pending_pr = {
            "number": 43,
            "state": "OPEN",
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "CI",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                }
            ],
        }
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: pending_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/test-pending-checks",
            require_github_pr=True,
        )

        ci_check = next(
            (c for c in result["checks"] if c["name"] == "github_checks"), None
        )
        assert ci_check is not None
        assert ci_check["status"] == "FAIL", "Pending CI checks must not pass merge readiness"

    def test_green_github_checks_pass(self, env, monkeypatch):
        """
        GitHub PR exists and all CI checks are COMPLETED/SUCCESS → github_checks=PASS.
        """
        self._setup_completed_pr(env)

        green_pr = {
            "number": 44,
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "CI",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
        }
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: green_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/test-green-checks",
            require_github_pr=True,
        )

        ci_check = next(
            (c for c in result["checks"] if c["name"] == "github_checks"), None
        )
        assert ci_check is not None
        assert ci_check["status"] == "PASS", "All green CI checks must produce PASS"

        pr_check = next(
            (c for c in result["checks"] if c["name"] == "github_pr_exists"), None
        )
        assert pr_check is not None
        assert pr_check["status"] == "PASS"

    def test_github_not_checked_without_require_flag(self, env, monkeypatch):
        """
        Without require_github_pr=True, GitHub PR/CI checks are not performed.
        Backward compatibility: existing callers without branch argument are unaffected.
        """
        self._setup_completed_pr(env)

        # This would raise if called, ensuring the test catches any unexpected call
        def _raise(*args, **kwargs):
            raise AssertionError("_find_branch_pr must not be called without require_github_pr")

        monkeypatch.setattr(cv, "_find_branch_pr", _raise)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            # No branch, no require_github_pr
        )

        github_checks = [c for c in result["checks"] if c["name"].startswith("github_")]
        assert not github_checks, (
            "No github_ checks must appear when require_github_pr is not set"
        )

    def test_result_includes_github_pr_in_payload(self, env, monkeypatch):
        """
        verify_pr_closure payload includes 'github_pr' key when GitHub check was run.
        """
        self._setup_completed_pr(env)

        mock_pr = {"number": 50, "state": "OPEN", "statusCheckRollup": []}
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: mock_pr)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/test-payload",
            require_github_pr=True,
        )

        assert "github_pr" in result, "Result payload must include 'github_pr' key"
        assert result["github_pr"]["number"] == 50

    def test_branch_in_result_payload(self, env, monkeypatch):
        """
        verify_pr_closure result payload includes 'branch' key when provided.
        """
        self._setup_completed_pr(env)
        monkeypatch.setattr(cv, "_find_branch_pr", lambda branch: None)

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=env["feature_plan"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts"],
            state_dir=env["state_dir"],
            branch="feature/my-branch",
            require_github_pr=True,
        )

        assert result.get("branch") == "feature/my-branch"
