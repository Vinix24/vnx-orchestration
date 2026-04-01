#!/usr/bin/env python3
"""PR-3 Certification tests — deterministic queue state reconciliation.

Certifies that the reconciliation system implemented in PR-0 through PR-2
prevents the queue drift observed during the double-feature trial and
produces auditable queue truth during real autonomous execution.

Test categories:
  - Unit tests: derive PR states from dispatch directories and receipts
  - Integration tests: reconcile fixes stale projections, PR_QUEUE.md matches,
    kickoff/promotion refresh, per-PR closure gate evidence
  - Certification tests: reproduce double-feature trial drift, verify reconciled
    truth before promotion, verify Gemini/Codex evidence non-contradictory
  - Governance tests: T0 instructions mention reconciliation, kickoff preflight
    blocks on drift

Dispatch-ID: 20260331-212208-certification-with-gemini-revi-C
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from queue_reconciler import (
    QueueReconciler,
    ReconcileResult,
    parse_feature_plan,
    scan_dispatch_dirs,
    load_receipt_dispatch_ids,
)
from kickoff_preflight import run_preflight
import closure_verifier as cv
from review_contract import (
    Deliverable,
    QualityGate,
    ReviewContract,
    TestEvidence,
)


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

FOUR_PR_FEATURE_PLAN = """\
# Feature: Deterministic Queue State Reconciliation

**Status**: Active
**Risk-Class**: high

## PR-0: Queue Truth Contract
**Track**: C
**Priority**: P1
**Skill**: @architect
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: []

`gate_pr0_queue_truth_contract`

---

## PR-1: Reconcile Queue State
**Track**: B
**Priority**: P1
**Skill**: @backend-developer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: [PR-0]

`gate_pr1_queue_reconciliation`

---

## PR-2: Kickoff And Closure Integration
**Track**: C
**Priority**: P2
**Skill**: @quality-engineer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: [PR-1]

`gate_pr2_kickoff_queue_integration`

---

## PR-3: Certification
**Track**: C
**Priority**: P1
**Skill**: @quality-engineer
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Dependencies**: [PR-2]

`gate_pr3_queue_reconciliation_certification`

---
"""


def _write_dispatch(path: Path, pr_id: str, dispatch_id: str, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[[TARGET:B]]\nManager Block\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n{extra}\n"
    )


def _write_receipt(receipts_file: Path, dispatch_id: str, event_type: str = "task_complete") -> None:
    receipts_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"dispatch_id": dispatch_id, "event_type": event_type, "status": "success"}
    with receipts_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _make_feature_plan(tmp_path: Path, content: str = FOUR_PR_FEATURE_PLAN) -> Path:
    fp = tmp_path / "FEATURE_PLAN.md"
    fp.write_text(content)
    return fp


def _make_projection(tmp_path: Path, pr_states: Dict[str, str]) -> Path:
    status_map = {"completed": "completed", "active": "in_progress", "pending": "queued", "blocked": "blocked"}
    prs = [{"id": pid, "title": pid, "status": status_map.get(s, s)} for pid, s in pr_states.items()]
    payload = {
        "prs": prs,
        "completed": [p for p, s in pr_states.items() if s == "completed"],
        "active": [p for p, s in pr_states.items() if s == "active"],
        "blocked": [p for p, s in pr_states.items() if s == "blocked"],
    }
    proj = tmp_path / "state" / "pr_queue_state.json"
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.write_text(json.dumps(payload))
    return proj


def _make_contract(pr_id: str, review_stack: Optional[List[str]] = None, content_hash: str = "abcdef1234567890"):
    if review_stack is None:
        review_stack = ["gemini_review", "codex_gate"]
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Test PR",
        feature_title="Test Feature",
        branch="feature/test",
        track="C",
        risk_class="high",
        merge_policy="human",
        review_stack=list(review_stack),
        closure_stage="in_review",
        deliverables=[Deliverable(description="test", category="implementation")],
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


def _write_clean_gate_evidence(tmp_path: Path, results_dir: Path, pr_id: str, content_hash: str = "abcdef1234567890"):
    """Write consistent passing Gemini + Codex gate evidence for a PR."""
    reports_dir = tmp_path / "reports" / pr_id
    reports_dir.mkdir(parents=True, exist_ok=True)

    gemini_report = reports_dir / "gemini.md"
    gemini_report.write_text("# Gemini Review\nAll clear. No blocking issues.\n")
    codex_report = reports_dir / "codex.md"
    codex_report.write_text("# Codex Gate\nAll clear.\n")

    _write_gate_result(results_dir, "gemini_review", pr_id, {
        "gate": "gemini_review", "pr_id": pr_id, "status": "pass",
        "blocking_count": 0, "advisory_count": 0,
        "contract_hash": content_hash,
        "report_path": str(gemini_report),
    })
    _write_gate_result(results_dir, "codex_gate", pr_id, {
        "gate": "codex_gate", "pr_id": pr_id, "verdict": "pass",
        "required": True, "contract_hash": content_hash,
        "content_hash": content_hash,
        "report_path": str(codex_report),
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set up a full VNX-like environment for certification tests."""
    project_root = tmp_path / "repo"
    project_root.mkdir()

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
        "dispatch_dir": dispatch_dir,
        "state_dir": state_dir,
        "receipts_file": receipts,
        "tmp_path": tmp_path,
    }


# ===========================================================================
# UNIT TESTS — derive PR state from dispatch directories
# ===========================================================================


class TestDeriveActivePR:
    """Derive active PR from dispatch directories."""

    def test_single_active_dispatch(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "active" / "d0.md", "PR-0", "d0")

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "receipts.ndjson",
            feature_plan=fp,
        )
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-0"] == "active"
        pr0 = next(p for p in result.prs if p.pr_id == "PR-0")
        assert pr0.provenance["source"] == "dispatch_filesystem"
        assert pr0.provenance["dir"] == "active"

    def test_active_dispatch_blocks_dependents(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "active" / "d0.md", "PR-0", "d0")

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "receipts.ndjson",
            feature_plan=fp,
        )
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-1"] == "blocked"
        assert states["PR-2"] == "blocked"
        assert states["PR-3"] == "blocked"


class TestDeriveCompletedPRs:
    """Derive completed PRs from dispatch directories and receipts."""

    def test_completed_with_receipt(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        receipts = tmp_path / "receipts.ndjson"
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(receipts, "d0")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=receipts, feature_plan=fp)
        result = r.reconcile()
        pr0 = next(p for p in result.prs if p.pr_id == "PR-0")
        assert pr0.state == "completed"
        assert pr0.provenance["receipt_confirmed"] is True

    def test_completed_without_receipt_is_unconfirmed(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        pr0 = next(p for p in result.prs if p.pr_id == "PR-0")
        assert pr0.state == "completed"
        assert pr0.provenance["unconfirmed_completion"] is True

    def test_chain_completion_unblocks_downstream(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        for pr_id, did in [("PR-0", "d0"), ("PR-1", "d1"), ("PR-2", "d2")]:
            _write_dispatch(dispatch_dir / "completed" / f"{did}.md", pr_id, did)

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-0"] == "completed"
        assert states["PR-1"] == "completed"
        assert states["PR-2"] == "completed"
        assert states["PR-3"] == "pending"  # deps satisfied


class TestDeriveWaitingAndBlockedPRs:
    """Derive waiting and blocked PRs from dependency graph."""

    def test_no_dispatches_first_pr_pending_rest_blocked(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        r = QueueReconciler(
            dispatch_dir=tmp_path / "dispatches",
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
        )
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-0"] == "pending"
        assert states["PR-1"] == "blocked"
        assert states["PR-2"] == "blocked"
        assert states["PR-3"] == "blocked"

    def test_partial_completion_unblocks_next(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-1"] == "pending"
        assert states["PR-2"] == "blocked"
        assert states["PR-3"] == "blocked"

    def test_blocked_pr_has_provenance(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        r = QueueReconciler(
            dispatch_dir=tmp_path / "dispatches",
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
        )
        result = r.reconcile()
        pr3 = next(p for p in result.prs if p.pr_id == "PR-3")
        assert pr3.provenance["source"] == "feature_plan_dependency_graph"
        assert "PR-2" in pr3.provenance["blocking_dependencies"]


class TestIgnoreForeignAndStaleDispatches:
    """Ignore foreign or stale staging dispatches."""

    def test_foreign_pr_dispatch_does_not_affect_queue(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "active" / "foreign.md", "PR-99", "foreign-dispatch")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        # PR-99 does not appear in derived states
        assert "PR-99" not in states
        # Valid PRs retain correct state
        assert states["PR-0"] == "pending"
        # A warning is raised for the foreign dispatch
        foreign_warnings = [w for w in result.drift_warnings if "PR-99" in str(w.pr_id) or "PR-99" in w.message]
        assert len(foreign_warnings) >= 1

    def test_staging_dispatch_does_not_make_pr_active(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "staging" / "staged.md", "PR-0", "staged-d0")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        states = {p.pr_id: p.state for p in result.prs}
        assert states["PR-0"] == "pending"  # staging does not mean active

    def test_rejected_dispatch_does_not_complete(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "rejected" / "rej.md", "PR-0", "rejected-d0")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()
        assert next(p for p in result.prs if p.pr_id == "PR-0").state == "pending"


# ===========================================================================
# INTEGRATION TESTS — reconciliation fixes visible queue status
# ===========================================================================


class TestReconcileFixesStaleProjection:
    """Active dispatch exists while projected state is stale → reconcile fixes visible queue status."""

    def test_active_dispatch_but_projection_says_pending(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "active" / "d0.md", "PR-0", "d0")
        proj = _make_projection(tmp_path, {
            "PR-0": "pending", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp, projection_file=proj)
        result = r.reconcile()

        assert result.has_blocking_drift
        pr0 = next(p for p in result.prs if p.pr_id == "PR-0")
        assert pr0.state == "active"
        blocking = [w for w in result.drift_warnings if w.severity == "blocking"]
        assert any(w.pr_id == "PR-0" and w.derived_state == "active" for w in blocking)

    def test_completed_dispatch_but_projection_says_active(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        proj = _make_projection(tmp_path, {
            "PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp, projection_file=proj)
        result = r.reconcile()

        assert result.has_blocking_drift
        pr0 = next(p for p in result.prs if p.pr_id == "PR-0")
        assert pr0.state == "completed"

    def test_repair_eliminates_drift(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        _make_projection(tmp_path, {"PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked"})

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=state_dir / "pr_queue_state.json",
        )
        result = r.reconcile()
        assert result.has_blocking_drift

        from reconcile_queue_state import repair_projections
        repair_projections(result, state_dir, tmp_path)

        # Re-reconcile with repaired projection — no blocking drift
        r2 = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=state_dir / "pr_queue_state.json",
        )
        result2 = r2.reconcile()
        assert not result2.has_blocking_drift


class TestPRQueueMdMatchesReconciledState:
    """PR_QUEUE.md projection matches reconciled status summary."""

    def test_regenerated_md_reflects_derived_state(self, tmp_path):
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        _write_dispatch(dispatch_dir / "completed" / "d1.md", "PR-1", "d1")
        _write_dispatch(dispatch_dir / "active" / "d2.md", "PR-2", "d2")

        r = QueueReconciler(dispatch_dir=dispatch_dir, receipts_file=tmp_path / "r.ndjson", feature_plan=fp)
        result = r.reconcile()

        from reconcile_queue_state import repair_projections
        repair_projections(result, state_dir, tmp_path)

        md = (tmp_path / "PR_QUEUE.md").read_text()
        assert "Complete: 2" in md
        assert "Active: 1" in md
        assert "PR-0" in md and "PR-1" in md
        assert "🔄" in md or "Currently Active" in md
        assert "queue_reconciler" in md


class TestKickoffPreflightRefreshesQueueTruth:
    """Kickoff and promotion paths refresh queue truth before acting."""

    def test_preflight_detects_stale_queue(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "active" / "d0.md", "PR-0", "d0")
        _make_projection(env["project_root"] / ".vnx-data", {
            "PR-0": "pending", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert result["safe_to_promote"] is False
        assert len(result["blocking_drift"]) > 0

    def test_preflight_passes_when_queue_is_fresh(self, env):
        fp = _make_feature_plan(env["project_root"])
        # No dispatches, no stale projection
        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert result["safe_to_promote"] is True

    def test_preflight_returns_pr_status(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")

        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
            pr_id="PR-0",
        )
        assert result["safe_to_promote"] is True
        assert result["pr_status"]["state"] == "completed"


class TestPerPRClosureWithGateEvidence:
    """Per-PR closure succeeds only when gate evidence is present and internally consistent."""

    def test_closure_passes_with_complete_evidence(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        results_dir = env["tmp_path"] / "results"
        _write_clean_gate_evidence(env["tmp_path"], results_dir, "PR-0")
        contract = _make_contract("PR-0")

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )
        assert result["verdict"] == "pass"

    def test_closure_fails_without_gate_evidence(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        contract = _make_contract("PR-0")
        empty_results = env["tmp_path"] / "empty_results"
        empty_results.mkdir()

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=empty_results,
        )
        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "gate_gemini_review" in failed or "gate_codex_gate" in failed

    def test_closure_fails_on_incomplete_pr(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "active" / "d0.md", "PR-0", "d0")

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
        )
        assert result["verdict"] == "fail"
        assert result["reconciled_state"]["state"] == "active"


# ===========================================================================
# CERTIFICATION TESTS — reproduce trial drift and prove reconciliation
# ===========================================================================


class TestDoubleFeatureTrialDriftReproduction:
    """Reproduce the double-feature trial drift condition and verify reconciliation corrects it."""

    def test_trial_drift_active_dispatch_invisible_in_projection(self, tmp_path):
        """Trial scenario: dispatch is active but projection shows 'In Progress: None'.

        This is the exact condition observed during the double-feature trial.
        The reconciler must detect this as blocking drift and correct it.
        """
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Simulate trial condition: PR-0 completed, PR-1 active in filesystem
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        _write_dispatch(dispatch_dir / "active" / "d1.md", "PR-1", "d1")

        # Stale projection from before PR-1 was dispatched (shows no active)
        stale_proj = _make_projection(tmp_path, {
            "PR-0": "completed", "PR-1": "pending", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=stale_proj,
        )
        result = r.reconcile()

        # Reconciler must detect the drift
        assert result.has_blocking_drift
        pr1 = next(p for p in result.prs if p.pr_id == "PR-1")
        assert pr1.state == "active"

        blocking = [w for w in result.drift_warnings if w.pr_id == "PR-1" and w.severity == "blocking"]
        assert len(blocking) == 1
        assert blocking[0].derived_state == "active"
        assert blocking[0].projected_state == "pending"

    def test_trial_drift_completed_still_shown_active(self, tmp_path):
        """Trial scenario: dispatch completed but projection still shows active.

        T0 would unnecessarily block downstream PRs.
        """
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"

        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        _write_dispatch(dispatch_dir / "completed" / "d1.md", "PR-1", "d1")

        stale_proj = _make_projection(tmp_path, {
            "PR-0": "completed", "PR-1": "active", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=stale_proj,
        )
        result = r.reconcile()

        assert result.has_blocking_drift
        pr1 = next(p for p in result.prs if p.pr_id == "PR-1")
        assert pr1.state == "completed"
        # PR-2 should be pending now, not blocked
        pr2 = next(p for p in result.prs if p.pr_id == "PR-2")
        assert pr2.state == "pending"

    def test_trial_drift_multi_pr_cascade(self, tmp_path):
        """Trial scenario: multiple PRs have drifted simultaneously.

        Simulates the state after multiple rapid completions where projection
        was not refreshed between them.
        """
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"

        # All of PR-0 through PR-2 completed, PR-3 active
        for pr_id, did in [("PR-0", "d0"), ("PR-1", "d1"), ("PR-2", "d2")]:
            _write_dispatch(dispatch_dir / "completed" / f"{did}.md", pr_id, did)
        _write_dispatch(dispatch_dir / "active" / "d3.md", "PR-3", "d3")

        # Massively stale projection: still shows PR-0 as active
        stale_proj = _make_projection(tmp_path, {
            "PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=stale_proj,
        )
        result = r.reconcile()

        assert result.has_blocking_drift
        states = {p.pr_id: p.state for p in result.prs}
        assert states == {"PR-0": "completed", "PR-1": "completed", "PR-2": "completed", "PR-3": "active"}


class TestReconciledTruthBeforePromotion:
    """Verify reconciled truth before next promotion."""

    def test_preflight_blocks_promotion_on_drift(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        # Stale projection claims PR-0 still active
        _make_projection(env["project_root"] / ".vnx-data", {
            "PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert result["safe_to_promote"] is False

    def test_preflight_passes_after_repair(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _make_projection(env["project_root"] / ".vnx-data", {
            "PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        # First preflight — blocks
        result1 = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
            repair=True,  # auto-repair
        )
        assert result1["safe_to_promote"] is False

        # Second preflight — projection is now repaired, should pass
        result2 = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert result2["safe_to_promote"] is True

    def test_source_of_truth_vs_projection_side_by_side(self, tmp_path):
        """Certification evidence: reconciled result shows both derived and projected state."""
        fp = _make_feature_plan(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _write_dispatch(dispatch_dir / "completed" / "d0.md", "PR-0", "d0")
        stale_proj = _make_projection(tmp_path, {
            "PR-0": "active", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })

        r = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=tmp_path / "r.ndjson",
            feature_plan=fp,
            projection_file=stale_proj,
        )
        result = r.reconcile()
        result_dict = result.as_dict()

        # Derived state is in prs[].state
        pr0_derived = next(p for p in result_dict["prs"] if p["pr_id"] == "PR-0")
        assert pr0_derived["state"] == "completed"

        # Drift warning captures the projected state for side-by-side comparison
        drift = next(w for w in result_dict["drift_warnings"] if w["pr_id"] == "PR-0")
        assert drift["derived_state"] == "completed"
        assert drift["projected_state"] == "active"
        assert drift["severity"] == "blocking"


class TestGeminiCodexEvidenceNonContradictory:
    """Verify Gemini and Codex evidence surfaces are branch-local and non-contradictory."""

    def test_consistent_evidence_passes_closure(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        results_dir = env["tmp_path"] / "results"
        _write_clean_gate_evidence(env["tmp_path"], results_dir, "PR-0")
        contract = _make_contract("PR-0")

        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )
        assert result["verdict"] == "pass"
        contradiction_checks = [c for c in result["checks"] if c["name"].startswith("contradiction_")]
        assert all(c["status"] == "PASS" for c in contradiction_checks)

    def test_contradictory_gemini_pass_with_blocking_report_fails(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        results_dir = env["tmp_path"] / "results"
        reports_dir = env["tmp_path"] / "reports"
        reports_dir.mkdir(parents=True)

        # Gemini report has blocking findings
        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text(
            "# Gemini Review\n\n## Findings\n"
            "- [BLOCKING] Race condition in reconciler\n"
            "- [BLOCKING] Missing error handling\n"
        )
        # But gate result says pass
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")
        _write_gate_result(results_dir, "codex_gate", "PR-0", {
            "gate": "codex_gate", "pr_id": "PR-0", "verdict": "pass",
            "required": True, "contract_hash": "abcdef1234567890",
            "content_hash": "abcdef1234567890",
            "report_path": str(codex_report),
        })

        contract = _make_contract("PR-0")
        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )
        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "contradiction_gemini_review" in failed

    def test_contradictory_codex_fail_with_clean_report_fails(self, env):
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        results_dir = env["tmp_path"] / "results"
        reports_dir = env["tmp_path"] / "reports"
        reports_dir.mkdir(parents=True)

        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "abcdef1234567890",
            "report_path": str(gemini_report),
        })

        # Codex report is clean but gate says fail
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear. No issues found.\n")
        _write_gate_result(results_dir, "codex_gate", "PR-0", {
            "gate": "codex_gate", "pr_id": "PR-0", "status": "fail",
            "verdict": "fail", "blocking_count": 2,
            "required": True, "contract_hash": "abcdef1234567890",
            "content_hash": "abcdef1234567890",
            "report_path": str(codex_report),
        })

        contract = _make_contract("PR-0")
        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )
        assert result["verdict"] == "fail"

    def test_hash_mismatch_detected(self, env):
        """Gate evidence with stale content hash is caught."""
        fp = _make_feature_plan(env["project_root"])
        _write_dispatch(env["dispatch_dir"] / "completed" / "d0.md", "PR-0", "d0")
        _write_receipt(env["receipts_file"], "d0")

        results_dir = env["tmp_path"] / "results"
        reports_dir = env["tmp_path"] / "reports"
        reports_dir.mkdir(parents=True)

        gemini_report = reports_dir / "gemini.md"
        gemini_report.write_text("# Gemini Review\nAll clear.\n")
        _write_gate_result(results_dir, "gemini_review", "PR-0", {
            "gate": "gemini_review", "pr_id": "PR-0", "status": "pass",
            "blocking_count": 0, "advisory_count": 0,
            "contract_hash": "STALE_HASH_FROM_PREVIOUS_RUN",
            "report_path": str(gemini_report),
        })
        codex_report = reports_dir / "codex.md"
        codex_report.write_text("# Codex Gate\nAll clear.\n")
        _write_gate_result(results_dir, "codex_gate", "PR-0", {
            "gate": "codex_gate", "pr_id": "PR-0", "verdict": "pass",
            "required": True,
            "contract_hash": "abcdef1234567890",
            "content_hash": "abcdef1234567890",
            "report_path": str(codex_report),
        })

        contract = _make_contract("PR-0", content_hash="abcdef1234567890")
        result = cv.verify_pr_closure(
            pr_id="PR-0",
            project_root=env["project_root"],
            feature_plan=fp,
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            state_dir=env["state_dir"],
            review_contract=contract,
            gate_results_dir=results_dir,
        )
        assert result["verdict"] == "fail"
        failed = {c["name"] for c in result["checks"] if c["status"] == "FAIL"}
        assert "hash_gemini_review" in failed


# ===========================================================================
# GOVERNANCE TESTS — T0 instructions and kickoff handoff
# ===========================================================================


class TestGovernanceInstructions:
    """T0 instructions mention queue reconciliation on mismatch."""

    def test_t0_orchestrator_mentions_reconciliation(self):
        skill_path = VNX_ROOT / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("T0 orchestrator skill not found in this worktree")
        content = skill_path.read_text(encoding="utf-8")
        assert "reconcil" in content.lower(), (
            "T0 orchestrator SKILL.md must mention queue reconciliation"
        )

    def test_t0_orchestrator_mentions_promotion_guard(self):
        skill_path = VNX_ROOT / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("T0 orchestrator skill not found in this worktree")
        content = skill_path.read_text(encoding="utf-8")
        assert "promot" in content.lower(), (
            "T0 orchestrator SKILL.md must reference promotion in context of queue truth"
        )

    def test_queue_truth_contract_exists_and_is_canonical(self):
        contract_path = VNX_ROOT / "docs" / "core" / "70_QUEUE_TRUTH_CONTRACT.md"
        if not contract_path.exists():
            pytest.skip("Queue truth contract not found in this worktree")
        content = contract_path.read_text(encoding="utf-8")
        assert "Status**: Canonical" in content or "Status: Canonical" in content.replace("*", "")
        assert "Source-Of-Truth Hierarchy" in content
        assert "Drift Detection" in content


class TestKickoffPreflightGovernance:
    """Kickoff preflight blocks on drift and surfaces reconciled state."""

    def test_kickoff_preflight_exists(self):
        preflight_path = SCRIPTS_DIR / "kickoff_preflight.py"
        assert preflight_path.exists(), "kickoff_preflight.py must exist"

    def test_kickoff_preflight_returns_reconciled_state(self, env):
        fp = _make_feature_plan(env["project_root"])
        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert "reconciled_state" in result
        assert result["reconciled_state"] is not None
        assert "prs" in result["reconciled_state"]

    def test_kickoff_preflight_exit_code_semantics(self, env):
        """Preflight returns structured result with safe_to_promote boolean."""
        fp = _make_feature_plan(env["project_root"])

        # Clean state → safe
        clean = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert clean["safe_to_promote"] is True

        # Introduce drift → unsafe
        _write_dispatch(env["dispatch_dir"] / "active" / "d0.md", "PR-0", "d0")
        _make_projection(env["project_root"] / ".vnx-data", {
            "PR-0": "pending", "PR-1": "blocked", "PR-2": "blocked", "PR-3": "blocked",
        })
        drifted = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
        )
        assert drifted["safe_to_promote"] is False

    def test_kickoff_preflight_unknown_pr_is_blocked(self, env):
        fp = _make_feature_plan(env["project_root"])
        result = run_preflight(
            project_root=env["project_root"],
            dispatch_dir=env["dispatch_dir"],
            receipts_file=env["receipts_file"],
            feature_plan=fp,
            state_dir=env["state_dir"],
            pr_id="PR-99",
        )
        assert result["safe_to_promote"] is False
        assert any("PR-99" in str(w) for w in result["blocking_drift"])
