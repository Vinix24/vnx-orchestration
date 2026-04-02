#!/usr/bin/env python3
"""Multi-feature autonomous chain certification tests (PR-4, Feature 14).

Proves that governed unattended multi-feature chains can:
  1. Advance only after merged green-CI feature completion
  2. Handle recoverable interruptions without destroying chain continuity
  3. Maintain carry-forward findings across the full certified run
  4. Enforce deferred item validation (O-3 blocker rejection, reason requirement)
  5. Execute a 5-feature chain lifecycle end-to-end with audit trail

This is the final certification suite for Feature 14.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from chain_state_projection import (
    BLOCKED_STATES,
    build_carry_forward_summary,
    build_chain_projection,
    compute_advancement_truth,
    init_chain_state,
    record_state_transition,
)
from chain_recovery import (
    FAILURE_CLASS_FIXABLE,
    FAILURE_CLASS_NON_RECOVERABLE,
    FAILURE_CLASS_TRANSIENT,
    MAX_TOTAL_ATTEMPTS,
    RECOVERY_ACTION_ESCALATE,
    RECOVERY_ACTION_REQUEUE,
    RESUME_SAFE_STATES,
    RESUME_UNSAFE_STATES,
    _is_ancestor,
    _sha_prefix_matches,
    _validate_deferred_items,
    build_next_feature_context,
    check_branch_baseline,
    classify_failure,
    evaluate_recovery,
    is_resume_safe,
    record_failure_attempt,
    snapshot_feature_boundary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    (tmp_path / "review_gates" / "results").mkdir(parents=True)
    return tmp_path


def _write_pr_queue(sd: Path, prs: List[Dict[str, Any]]) -> None:
    (sd / "pr_queue_state.json").write_text(
        json.dumps({"feature": "Certification", "feature_metadata": {}, "prs": prs})
    )


def _write_open_items(sd: Path, items: List[Dict[str, Any]]) -> None:
    (sd / "open_items.json").write_text(
        json.dumps({"schema_version": "1", "items": items, "next_id": len(items) + 1})
    )


def _write_gate(sd: Path, pr_num: int, gate: str, status: str = "approve",
                 blocking: int = 0, contract_hash: str = "h123") -> None:
    result = {
        "gate": gate, "pr_number": pr_num, "pr_id": str(pr_num), "status": status,
        "blocking_count": blocking, "contract_hash": contract_hash,
        "report_path": f"/tmp/r-{pr_num}-{gate}.md", "recorded_at": "2026-04-02T12:00:00Z",
    }
    (sd / "review_gates" / "results" / f"pr-{pr_num}-{gate}.json").write_text(json.dumps(result))


def _certify_gates(sd: Path, pr_num: int) -> None:
    _write_gate(sd, pr_num, "gemini_review")
    _write_gate(sd, pr_num, "codex_gate")


def _init_five_feature_chain(sd: Path) -> None:
    """Initialize a 5-feature chain: PR-0 -> PR-1 -> PR-2 -> PR-3 -> PR-4."""
    prs = [
        {"id": "PR-0", "status": "completed", "dependencies": [], "title": "Contract", "track": "C", "gate": "g0"},
        {"id": "PR-1", "status": "completed", "dependencies": ["PR-0"], "title": "Projection", "track": "B", "gate": "g1"},
        {"id": "PR-2", "status": "completed", "dependencies": ["PR-1"], "title": "Recovery", "track": "B", "gate": "g2"},
        {"id": "PR-3", "status": "completed", "dependencies": ["PR-2"], "title": "Carry-Forward", "track": "C", "gate": "g3"},
        {"id": "PR-4", "status": "queued", "dependencies": ["PR-3"], "title": "Certification", "track": "C", "gate": "g4"},
    ]
    _write_pr_queue(sd, prs)
    init_chain_state(
        sd, chain_id="cert-final-001", feature_plan="FEATURE_PLAN.md",
        feature_sequence=["PR-0", "PR-1", "PR-2", "PR-3", "PR-4"],
        chain_origin_sha="aaa000",
    )


# ---------------------------------------------------------------------------
# 1. Advancement only after merged green-CI feature completion
# ---------------------------------------------------------------------------

class TestAdvancementAfterMerge:
    """Chain cannot advance unless PR merged + gates certified + no blockers."""

    def test_advancement_blocked_when_pr_not_merged(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "queued", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _certify_gates(state_dir, 0)
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-0",
        )
        assert result["can_advance"] is False
        assert any("not yet merged" in b for b in result["blockers"])

    def test_advancement_blocked_when_gemini_rejects(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _write_gate(state_dir, 0, "gemini_review", status="reject", blocking=2)
        _write_gate(state_dir, 0, "codex_gate", status="approve")
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-0",
        )
        assert result["can_advance"] is False
        assert "not_certified" in result["certification_status"]["gemini_review"]

    def test_advancement_blocked_when_codex_has_blocking_findings(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _certify_gates(state_dir, 0)
        # Override codex with blocking findings
        _write_gate(state_dir, 0, "codex_gate", status="approve", blocking=1)
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-0",
        )
        assert result["can_advance"] is False

    def test_advancement_succeeds_with_all_conditions_met(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _certify_gates(state_dir, 0)
        _write_open_items(state_dir, [])
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[], state_dir=state_dir, current_feature_id="PR-0",
        )
        assert result["can_advance"] is True
        assert result["blockers"] == []

    def test_advancement_blocked_by_blocker_open_item_even_with_gates_passing(self, state_dir: Path) -> None:
        _write_pr_queue(state_dir, [
            {"id": "PR-0", "status": "completed", "dependencies": [], "title": "T", "track": "C", "gate": "g"},
        ])
        _certify_gates(state_dir, 0)
        blocker = {"id": "OI-1", "severity": "blocker", "status": "open", "title": "Critical"}
        result = compute_advancement_truth(
            pr_queue=json.loads((state_dir / "pr_queue_state.json").read_text()),
            open_items=[blocker], state_dir=state_dir, current_feature_id="PR-0",
        )
        assert result["can_advance"] is False


# ---------------------------------------------------------------------------
# 2. Interruption and recovery under recoverable disruption
# ---------------------------------------------------------------------------

class TestInterruptionAndRecovery:
    """Recoverable chain interruption does not destroy continuity."""

    def test_transient_failure_requeues_and_preserves_chain(self, state_dir: Path) -> None:
        """Transient failure -> requeue -> chain continues."""
        _init_five_feature_chain(state_dir)
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-4")

        # Simulate transient failure
        record_state_transition(state_dir, to_state="FEATURE_FAILED", feature_id="PR-4",
                                  reason="provider timeout after 30s")
        record_state_transition(state_dir, to_state="RECOVERY_PENDING", feature_id="PR-4")

        # Evaluate recovery
        history: Dict[str, Any] = {}
        decision = evaluate_recovery("PR-4", "provider timeout after 30s", history)
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.must_start_from_main is True

        # Record attempt and requeue
        record_failure_attempt(history, "PR-4", decision.failure_class)
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-4",
                                  reason="requeue attempt 1")

        projection = build_chain_projection(state_dir)
        assert projection["chain_state"] == "FEATURE_ACTIVE"
        assert projection["is_blocked"] is False
        # Requeue history tracked
        assert projection["requeue_history"]["PR-4"]["total_attempts"] >= 1

    def test_fixable_failure_requeues_with_class_tracking(self, state_dir: Path) -> None:
        history: Dict[str, Any] = {}
        decision = evaluate_recovery("PR-2", "lint error in module.py", history)
        assert decision.action == RECOVERY_ACTION_REQUEUE
        assert decision.failure_class == FAILURE_CLASS_FIXABLE

        record_failure_attempt(history, "PR-2", decision.failure_class)
        assert history["PR-2"]["failure_classes"][FAILURE_CLASS_FIXABLE] == 1

    def test_escalation_after_max_retries_halts_chain(self, state_dir: Path) -> None:
        """After 3 total retries, chain must halt."""
        _init_five_feature_chain(state_dir)
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-4")

        history: Dict[str, Any] = {"PR-4": {"total_attempts": MAX_TOTAL_ATTEMPTS, "failure_classes": {}}}
        decision = evaluate_recovery("PR-4", "another timeout", history)
        assert decision.action == RECOVERY_ACTION_ESCALATE

        record_state_transition(state_dir, to_state="CHAIN_HALTED", feature_id="PR-4",
                                  reason=decision.reason)
        projection = build_chain_projection(state_dir)
        assert projection["chain_state"] == "CHAIN_HALTED"
        assert projection["is_blocked"] is True
        assert projection["is_recovery_needed"] is True

    def test_non_recoverable_failure_escalates_immediately(self, state_dir: Path) -> None:
        decision = evaluate_recovery("PR-1", "architectural incompatibility", {})
        assert decision.action == RECOVERY_ACTION_ESCALATE
        assert decision.failure_class == FAILURE_CLASS_NON_RECOVERABLE

    def test_chain_state_preserved_through_recovery_cycle(self, state_dir: Path) -> None:
        """Carry-forward ledger survives failure and recovery."""
        _init_five_feature_chain(state_dir)

        # PR-0 completed with findings
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="Contract",
            status="completed", prs_merged=["PR-0"],
            findings=[{"id": "F-REC-1", "severity": "warn", "resolution_status": "open"}],
        )
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1")
        record_state_transition(state_dir, to_state="FEATURE_FAILED", feature_id="PR-1")
        record_state_transition(state_dir, to_state="RECOVERY_PENDING", feature_id="PR-1")
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-1",
                                  reason="requeue")

        # Findings from PR-0 must still be visible
        ctx = build_next_feature_context(state_dir)
        assert ctx["open_finding_count"] >= 1

    def test_resume_safety_classification_complete(self) -> None:
        """All chain states classified as either resume-safe or resume-unsafe."""
        all_states = RESUME_SAFE_STATES | RESUME_UNSAFE_STATES
        from chain_state_projection import CHAIN_STATES
        # Every real state (excluding NOT_INITIALIZED sentinel) should be classified
        for state in CHAIN_STATES - {"NOT_INITIALIZED"}:
            assert state in all_states, f"state {state} not classified for resume safety"


# ---------------------------------------------------------------------------
# 3. Carry-forward across the full certified run
# ---------------------------------------------------------------------------

class TestCarryForwardFullRun:
    """Carry-forward findings remain visible and cumulative across the certified chain."""

    def test_five_feature_carry_forward_accumulation(self, state_dir: Path) -> None:
        """Each feature adds findings; all remain visible at chain end."""
        _init_five_feature_chain(state_dir)

        for i in range(5):
            pr_id = f"PR-{i}"
            snapshot_feature_boundary(
                state_dir, feature_id=pr_id, feature_name=f"Feature {i}",
                status="completed", prs_merged=[pr_id],
                findings=[{"id": f"F-{i}", "severity": "warn", "resolution_status": "open"}],
                open_items=[{"id": f"OI-{i}", "severity": "info", "status": "open", "title": f"Item {i}"}],
            )

        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert len(ledger["findings"]) == 5
        assert len(ledger["open_items"]) == 5
        assert len(ledger["feature_summaries"]) == 5

        ctx = build_next_feature_context(state_dir)
        assert ctx["features_completed"] == 5
        assert ctx["unresolved_item_count"] == 5
        assert ctx["open_finding_count"] == 5

    def test_carry_forward_summary_accurate_at_chain_end(self, state_dir: Path) -> None:
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            findings=[
                {"id": "F-A", "severity": "blocker", "resolution_status": "open"},
                {"id": "F-B", "severity": "warn", "resolution_status": "resolved"},
            ],
            open_items=[{"id": "OI-A", "severity": "warn", "status": "open", "title": "T"}],
            residual_risks=[{"risk": "perf"}],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        summary = build_carry_forward_summary(ledger, open_items=[])
        assert summary["total_findings"] == 2
        assert summary["open_findings"] == 1
        assert summary["blocker_findings"] == 1
        assert summary["residual_risks"] == 1
        assert summary["carry_forward_open_items"] == 1


# ---------------------------------------------------------------------------
# 4. Deferred item validation (linter-added enforcement)
# ---------------------------------------------------------------------------

class TestDeferredItemValidation:
    """Contract O-3 enforcement: blocker items cannot be deferred, reason required."""

    def test_blocker_deferral_raises_error(self) -> None:
        with pytest.raises(ValueError, match="Cannot defer blocker"):
            _validate_deferred_items(
                [{"id": "D-1", "severity": "blocker", "reason": "some reason"}], "PR-0"
            )

    def test_missing_reason_raises_error(self) -> None:
        with pytest.raises(ValueError, match="missing required 'reason'"):
            _validate_deferred_items(
                [{"id": "D-2", "severity": "warn"}], "PR-0"
            )

    def test_valid_deferral_passes(self) -> None:
        result = _validate_deferred_items(
            [{"id": "D-3", "severity": "warn", "reason": "deferred to next feature"}], "PR-0"
        )
        assert len(result) == 1
        assert result[0]["origin_feature"] == "PR-0"

    def test_deferred_items_in_snapshot(self, state_dir: Path) -> None:
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            deferred_items=[
                {"id": "D-4", "severity": "warn", "reason": "low priority, deferred"},
                {"id": "D-5", "severity": "info", "reason": "cosmetic"},
            ],
        )
        ledger = json.loads((state_dir / "chain_carry_forward.json").read_text())
        assert len(ledger["deferred_items"]) == 2
        assert ledger["feature_summaries"][0]["deferred_items_count"] == 2

    def test_blocker_deferral_in_snapshot_raises(self, state_dir: Path) -> None:
        with pytest.raises(ValueError, match="Cannot defer blocker"):
            snapshot_feature_boundary(
                state_dir, feature_id="PR-0", feature_name="F0",
                status="completed", prs_merged=["PR-0"],
                deferred_items=[{"id": "D-6", "severity": "blocker", "reason": "trying to sneak past"}],
            )


# ---------------------------------------------------------------------------
# 5. Branch baseline guard (linter-enhanced with ancestor check)
# ---------------------------------------------------------------------------

class TestBranchBaselineEnhanced:
    """Branch baseline guard with ancestor check (linter improvement)."""

    def _make_runner(self, merge_base_sha: str, is_ancestor: bool = False):
        def runner(args: list[str]) -> str:
            if "--is-ancestor" in args:
                if is_ancestor:
                    return ""
                raise RuntimeError("not an ancestor")
            if "merge-base" in args:
                return merge_base_sha
            raise RuntimeError(f"unexpected: {args}")
        return runner

    def test_ancestor_check_allows_newer_merge_base(self) -> None:
        """If merge-base differs but expected is ancestor of actual, accept."""
        expected = "aaa0000000000000000000000000000000000000"
        actual = "bbb1111111111111111111111111111111111111"
        result = check_branch_baseline(
            "feature/x", expected,
            git_runner=self._make_runner(actual, is_ancestor=True),
        )
        assert result.is_valid is True
        assert "descendant" in result.reason

    def test_sha_prefix_helper(self) -> None:
        assert _sha_prefix_matches("abc123", "abc1234567890") is True
        assert _sha_prefix_matches("abc123", "def456") is False
        assert _sha_prefix_matches("", "abc") is False


# ---------------------------------------------------------------------------
# 6. Full 5-feature chain lifecycle (end-to-end)
# ---------------------------------------------------------------------------

def _complete_feature(sd: Path, pr_idx: int, name: str, **snapshot_kwargs: Any) -> None:
    """Activate a feature, snapshot its boundary as completed, and advance."""
    pr_id = f"PR-{pr_idx}"
    record_state_transition(sd, to_state="FEATURE_ACTIVE", feature_id=pr_id)
    snapshot_feature_boundary(
        sd, feature_id=pr_id, feature_name=name,
        status="completed", prs_merged=[pr_id], merge_shas=[f"sha{pr_idx}"],
        gate_results={"gemini_review": "passed", "codex_gate": "passed"},
        **snapshot_kwargs,
    )
    record_state_transition(sd, to_state="FEATURE_ADVANCING", feature_id=pr_id)


def _recover_and_complete_feature(sd: Path, pr_idx: int, name: str, **snapshot_kwargs: Any) -> None:
    """Simulate transient failure, verify requeue, then complete the feature."""
    pr_id = f"PR-{pr_idx}"
    record_state_transition(sd, to_state="FEATURE_ACTIVE", feature_id=pr_id)
    record_state_transition(sd, to_state="FEATURE_FAILED", feature_id=pr_id, reason="provider timeout")
    record_state_transition(sd, to_state="RECOVERY_PENDING", feature_id=pr_id)
    decision = evaluate_recovery(pr_id, "provider timeout", {})
    assert decision.action == RECOVERY_ACTION_REQUEUE
    record_state_transition(sd, to_state="FEATURE_ACTIVE", feature_id=pr_id, reason="requeue after timeout")
    snapshot_feature_boundary(
        sd, feature_id=pr_id, feature_name=name,
        status="completed", prs_merged=[pr_id], merge_shas=[f"sha{pr_idx}"],
        gate_results={"gemini_review": "passed", "codex_gate": "passed"},
        **snapshot_kwargs,
    )
    record_state_transition(sd, to_state="FEATURE_ADVANCING", feature_id=pr_id)


def _assert_lifecycle_complete(sd: Path) -> None:
    """Assert chain completion, carry-forward integrity, and audit trail."""
    projection = build_chain_projection(sd)
    assert projection["chain_state"] == "CHAIN_COMPLETE"
    assert projection["is_blocked"] is False
    assert len(projection["completed_features"]) == 5

    ledger = json.loads((sd / "chain_carry_forward.json").read_text())
    assert len(ledger["feature_summaries"]) == 5
    assert any(f["id"] == "F-L-0" for f in ledger["findings"])

    oi = next(i for i in ledger["open_items"] if i["id"] == "OI-L-1")
    assert oi["status"] == "done"
    assert oi["origin_feature"] == "PR-1"

    assert len(ledger["residual_risks"]) == 1
    assert ledger["residual_risks"][0]["accepting_feature"] == "PR-2"

    audit = [json.loads(l) for l in (sd / "chain_audit.jsonl").read_text().strip().splitlines()]
    states = [r["to_state"] for r in audit]
    assert states[0] == "INITIALIZED"
    assert states[-1] == "CHAIN_COMPLETE"
    assert "FEATURE_FAILED" in states
    assert "RECOVERY_PENDING" in states
    recovery_idx = states.index("RECOVERY_PENDING")
    assert states[recovery_idx + 1] == "FEATURE_ACTIVE"


class TestFullChainLifecycle:
    """End-to-end: 5 features through init, active, advancing, with one recovery."""

    def test_five_feature_lifecycle_with_recovery(self, state_dir: Path) -> None:
        _init_five_feature_chain(state_dir)

        _complete_feature(state_dir, 0, "Contract",
            findings=[{"id": "F-L-0", "severity": "info", "resolution_status": "open"}])
        _complete_feature(state_dir, 1, "Projection",
            open_items=[{"id": "OI-L-1", "severity": "warn", "status": "open", "title": "Perf"}])
        _recover_and_complete_feature(state_dir, 2, "Recovery",
            residual_risks=[{"risk": "timeout sensitivity", "acceptance_rationale": "monitor"}])
        _complete_feature(state_dir, 3, "Carry-Forward",
            open_items=[{"id": "OI-L-1", "severity": "warn", "status": "done", "title": "Perf"}])

        _write_pr_queue(state_dir, [
            {"id": f"PR-{i}", "status": "completed", "dependencies": [f"PR-{i-1}"] if i else [],
             "title": t, "track": tr, "gate": f"g{i}"}
            for i, (t, tr) in enumerate([
                ("Contract", "C"), ("Projection", "B"), ("Recovery", "B"),
                ("Carry-Forward", "C"), ("Certification", "C"),
            ])
        ])
        record_state_transition(state_dir, to_state="FEATURE_ACTIVE", feature_id="PR-4")
        snapshot_feature_boundary(
            state_dir, feature_id="PR-4", feature_name="Certification",
            status="completed", prs_merged=["PR-4"], merge_shas=["sha4"],
            gate_results={"gemini_review": "passed", "codex_gate": "passed"},
        )
        record_state_transition(state_dir, to_state="CHAIN_COMPLETE", feature_id="PR-4")

        _assert_lifecycle_complete(state_dir)

    def test_chain_closes_with_zero_unresolved_blockers(self, state_dir: Path) -> None:
        """Feature 14 success criterion: zero unresolved chain-created open items."""
        _init_five_feature_chain(state_dir)
        # Complete all features, resolve all items
        snapshot_feature_boundary(
            state_dir, feature_id="PR-0", feature_name="F0",
            status="completed", prs_merged=["PR-0"],
            open_items=[{"id": "OI-Z-1", "severity": "warn", "status": "open", "title": "T"}],
        )
        snapshot_feature_boundary(
            state_dir, feature_id="PR-4", feature_name="F4",
            status="completed", prs_merged=["PR-4"],
            open_items=[{"id": "OI-Z-1", "severity": "warn", "status": "done", "title": "T"}],
        )
        ctx = build_next_feature_context(state_dir)
        assert ctx["unresolved_item_count"] == 0
        assert ctx["blocker_item_count"] == 0
