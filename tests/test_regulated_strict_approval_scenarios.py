#!/usr/bin/env python3
"""Scenario and enforcement tests for regulated_strict approval workflow (Feature 21, PR-1).

Covers contract-level behavioral scenarios from
docs/REGULATED_STRICT_GOVERNANCE_CONTRACT.md Section 8.1 and 8.3:

  - RA-4 enforcement: both pre and post approval required
  - Gate-pass does not auto-close (contract Section 8.3 item 2)
  - Timeout leads to PENDING_APPROVAL, not CLOSED (contract Section 8.3 item 3)
  - Automated approvals rejected (contract Section 8.1 item 3)
  - Approval metadata retention and queryability
  - Isolation: regulated_strict does not import from coding_strict/business_light paths
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from regulated_strict_approval import (
    ApprovalError,
    ApprovalState,
    ApprovalType,
    AutomatedApprovalError,
    ClosureBlockedError,
    ClosureType,
    DispatchApprovalState,
    InvalidStateTransitionError,
    RegulatedStrictApprovalPolicy,
    regulated_strict_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_policy() -> RegulatedStrictApprovalPolicy:
    return regulated_strict_policy()


def _make_pre_approval(
    dispatch_id: str = "d-001",
    approved_by: str = "operator",
    rationale: str = "Reviewed and approved for execution",
):
    policy = _make_policy()
    return policy.record_approval(
        dispatch_id=dispatch_id,
        approved_by=approved_by,
        rationale=rationale,
        approval_type=ApprovalType.PRE_EXECUTION,
    )


def _make_closure(
    dispatch_id: str = "d-001",
    closed_by: str = "operator",
    rationale: str = "Execution reviewed and accepted",
):
    policy = _make_policy()
    return policy.record_closure(
        dispatch_id=dispatch_id,
        closed_by=closed_by,
        rationale=rationale,
    )


def _state_at_pending_review(dispatch_id: str = "d-001") -> DispatchApprovalState:
    """Build a DispatchApprovalState that has been advanced to PENDING_REVIEW."""
    state = DispatchApprovalState(dispatch_id=dispatch_id)
    state.add_pre_approval(_make_pre_approval(dispatch_id=dispatch_id))
    state.transition_to(ApprovalState.APPROVED)
    state.transition_to(ApprovalState.EXECUTING)
    state.transition_to(ApprovalState.PENDING_REVIEW)
    return state


# ---------------------------------------------------------------------------
# RA-4 enforcement: both pre and post approval required
# ---------------------------------------------------------------------------

class TestRA4Enforcement:

    def setup_method(self) -> None:
        self.policy = _make_policy()

    def test_dispatch_requires_pre_execution_approval_before_closure(self) -> None:
        """Contract Section 8.1 item 1: dispatch requires pre-execution approval."""
        state = DispatchApprovalState(dispatch_id="d-001")
        state.state = ApprovalState.PENDING_REVIEW
        state.closure_record = _make_closure()
        # No pre-approval — cannot close
        assert not self.policy.can_close(state)

    def test_dispatch_requires_closure_record(self) -> None:
        """Contract Section 8.1 item 5: explicit closure approval required."""
        state = _state_at_pending_review()
        # Has pre-approval and is in correct state, but no closure record
        assert not self.policy.can_close(state)

    def test_both_approvals_required_for_close(self) -> None:
        """RA-4: both pre and post approval records must exist."""
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        assert self.policy.can_close(state)

    def test_only_pre_approval_insufficient_for_close(self) -> None:
        state = _state_at_pending_review()
        # Pre-approval present but no closure
        assert not self.policy.can_close(state)

    def test_only_closure_record_insufficient_for_close(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.state = ApprovalState.PENDING_REVIEW
        state.apply_closure(_make_closure())
        # Closure present but no pre-approval
        assert not self.policy.can_close(state)


# ---------------------------------------------------------------------------
# Gate-pass does not auto-close (contract Section 8.3 item 2)
# ---------------------------------------------------------------------------

class TestGatePassDoesNotAutoClose:

    def test_gate_pass_alone_does_not_close(self) -> None:
        """Simulate passing a gate without explicit approval — should not close."""
        policy = _make_policy()
        state = _state_at_pending_review()
        # No closure record recorded despite "gate passing"
        assert not policy.can_close(state)

    def test_receipt_arrival_does_not_close(self) -> None:
        """Simulates receipt arriving — dispatch stays in PENDING_REVIEW, not CLOSED."""
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        # Receipt arrived but no explicit closure — still in PENDING_REVIEW
        assert state.state == ApprovalState.PENDING_REVIEW
        assert not _make_policy().can_close(state)


# ---------------------------------------------------------------------------
# Timeout leads to PENDING_APPROVAL, not CLOSED (contract Section 8.3 item 3)
# ---------------------------------------------------------------------------

class TestTimeoutSemantics:

    def test_timeout_transitions_to_review_failed_then_pending_approval(self) -> None:
        """Simulate a timeout: dispatch fails review and loops back to PENDING_APPROVAL."""
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        state.transition_to(ApprovalState.PENDING_APPROVAL)
        assert state.state == ApprovalState.PENDING_APPROVAL

    def test_timeout_does_not_auto_close(self) -> None:
        """Timeout cannot directly transition to CLOSED."""
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.CLOSED)


# ---------------------------------------------------------------------------
# Automated approvals rejected (contract Section 8.1 item 3)
# ---------------------------------------------------------------------------

class TestAutomatedApprovalsRejected:

    def test_runtime_cannot_approve(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="runtime",
                rationale="auto",
            )

    def test_router_cannot_approve(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="router",
                rationale="auto",
            )

    def test_system_cannot_approve(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="system",
                rationale="auto",
            )

    def test_runtime_cannot_close(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_closure(
                dispatch_id="d-001",
                closed_by="runtime",
                rationale="auto-closed",
            )


# ---------------------------------------------------------------------------
# Approval metadata is retained and queryable
# ---------------------------------------------------------------------------

class TestApprovalMetadataRetention:

    def test_approval_id_is_stable(self) -> None:
        rec = _make_pre_approval()
        assert rec.approval_id == rec.approval_id  # identical on re-access

    def test_approval_metadata_all_fields_accessible(self) -> None:
        rec = _make_pre_approval(dispatch_id="d-meta", approved_by="T0",
                                  rationale="Verified by T0")
        assert rec.dispatch_id == "d-meta"
        assert rec.approved_by == "T0"
        assert rec.rationale == "Verified by T0"
        assert rec.approval_type == ApprovalType.PRE_EXECUTION

    def test_multiple_approvals_all_retained(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        rec1 = _make_pre_approval(approved_by="operator")
        rec2 = _make_pre_approval(approved_by="T0")
        state.add_pre_approval(rec1)
        state.add_pre_approval(rec2)
        assert len(state.pre_approvals) == 2
        assert state.pre_approvals[0].approved_by == "operator"
        assert state.pre_approvals[1].approved_by == "T0"

    def test_closure_metadata_retained(self) -> None:
        state = _state_at_pending_review()
        policy = _make_policy()
        closure = policy.record_closure(
            dispatch_id="d-001",
            closed_by="operator",
            rationale="All checks passed",
            closure_type=ClosureType.APPROVED,
            residual_risks=["minor gap"],
        )
        state.apply_closure(closure)
        assert state.closure_record is not None
        assert state.closure_record.rationale == "All checks passed"
        assert state.closure_record.closure_type == ClosureType.APPROVED
        assert "minor gap" in state.closure_record.residual_risks


# ---------------------------------------------------------------------------
# Isolation: regulated_strict does not import from coding_strict paths
# ---------------------------------------------------------------------------

class TestIsolation:

    def test_no_business_light_imports(self) -> None:
        """regulated_strict_approval must not depend on business_light_policy."""
        import regulated_strict_approval
        source = Path(regulated_strict_approval.__file__).read_text()
        assert "business_light_policy" not in source
        assert "from business_light_policy" not in source

    def test_no_governance_profile_selector_imports(self) -> None:
        """regulated_strict_approval must not depend on governance_profile_selector."""
        import regulated_strict_approval
        source = Path(regulated_strict_approval.__file__).read_text()
        assert "governance_profile_selector" not in source

    def test_module_can_be_imported_standalone(self) -> None:
        """Module should import without side effects."""
        import regulated_strict_approval  # noqa: F401 — import itself is the test
        assert True


# ---------------------------------------------------------------------------
# Dispatch-ID guard: cross-dispatch evidence rejected (RA-4 integrity)
# ---------------------------------------------------------------------------

class TestDispatchIdGuard:

    def test_add_pre_approval_rejects_wrong_dispatch_id(self) -> None:
        """Approval for a different dispatch must not satisfy RA-4 for this dispatch."""
        state = DispatchApprovalState(dispatch_id="d-target")
        foreign_approval = _make_pre_approval(dispatch_id="d-other")
        with pytest.raises(ApprovalError, match="d-other"):
            state.add_pre_approval(foreign_approval)

    def test_add_pre_approval_accepts_matching_dispatch_id(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-target")
        approval = _make_pre_approval(dispatch_id="d-target")
        state.add_pre_approval(approval)
        assert state.has_pre_execution_approval()

    def test_apply_closure_rejects_wrong_dispatch_id(self) -> None:
        """Closure for a different dispatch must not close this dispatch."""
        state = _state_at_pending_review(dispatch_id="d-target")
        foreign_closure = _make_closure(dispatch_id="d-other")
        with pytest.raises(ApprovalError, match="d-other"):
            state.apply_closure(foreign_closure)

    def test_apply_closure_accepts_matching_dispatch_id(self) -> None:
        state = _state_at_pending_review(dispatch_id="d-target")
        closure = _make_closure(dispatch_id="d-target")
        state.apply_closure(closure)
        assert state.has_closure_record()

    def test_mismatch_error_includes_both_ids(self) -> None:
        """Error message should name both the foreign and expected dispatch_id."""
        state = DispatchApprovalState(dispatch_id="d-expected")
        foreign_approval = _make_pre_approval(dispatch_id="d-foreign")
        with pytest.raises(ApprovalError, match="d-expected"):
            state.add_pre_approval(foreign_approval)


# ---------------------------------------------------------------------------
# RA-4 integrity guards: transition, retroactive approval, overwrite, re-approval
# ---------------------------------------------------------------------------

class TestRA4IntegrityGuards:

    # Guard 1: transition to APPROVED requires pre_approval
    def test_transition_to_approved_requires_pre_approval(self) -> None:
        """Cannot transition to APPROVED state without at least one pre-execution approval."""
        state = DispatchApprovalState(dispatch_id="d-001")
        with pytest.raises(ApprovalError, match="pre-execution approval"):
            state.transition_to(ApprovalState.APPROVED)

    def test_transition_to_approved_succeeds_with_pre_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        assert state.state == ApprovalState.APPROVED

    # Guard 2: retroactive approval blocked
    def test_add_pre_approval_rejected_after_approved(self) -> None:
        """Cannot add pre-execution approval once dispatch has moved past PENDING_APPROVAL."""
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        with pytest.raises(ApprovalError, match="retroactive"):
            state.add_pre_approval(_make_pre_approval())

    def test_add_pre_approval_rejected_in_executing(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        with pytest.raises(ApprovalError, match="retroactive"):
            state.add_pre_approval(_make_pre_approval())

    # Guard 3: closure overwrite blocked
    def test_apply_closure_rejects_overwrite(self) -> None:
        """A second closure record must not overwrite the first."""
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        with pytest.raises(ApprovalError, match="already has a closure"):
            state.apply_closure(_make_closure())

    # Guard 4: re-approval required after REVIEW_FAILED
    def test_review_failed_clears_pre_approvals(self) -> None:
        """After REVIEW_FAILED loops to PENDING_APPROVAL, pre_approvals are cleared."""
        state = _state_at_pending_review()
        assert state.has_pre_execution_approval()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        state.transition_to(ApprovalState.PENDING_APPROVAL)
        assert not state.has_pre_execution_approval()
        assert len(state.pre_approvals) == 0

    def test_review_failed_clears_closure_record(self) -> None:
        """After REVIEW_FAILED, any partial closure record is also cleared."""
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        state.transition_to(ApprovalState.REVIEW_FAILED)
        state.transition_to(ApprovalState.PENDING_APPROVAL)
        assert not state.has_closure_record()
        assert state.closure_record is None

    def test_review_failed_requires_fresh_approval_before_approved(self) -> None:
        """After reset, APPROVED transition requires a new pre-execution approval."""
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        state.transition_to(ApprovalState.PENDING_APPROVAL)
        # Pre-approvals were cleared — cannot transition to APPROVED without re-approval
        with pytest.raises(ApprovalError, match="pre-execution approval"):
            state.transition_to(ApprovalState.APPROVED)

    # Guard: CLOSED requires both pre_approval and closure_record
    def test_transition_to_closed_without_pre_approval_raises(self) -> None:
        """Cannot transition directly to CLOSED without a pre-execution approval (RA-4)."""
        state = DispatchApprovalState(dispatch_id="d-001")
        # Manually force to PENDING_REVIEW to isolate the CLOSED guard
        state.state = ApprovalState.PENDING_REVIEW
        state.closure_record = _make_closure()
        # No pre_approvals — must raise
        with pytest.raises(ApprovalError, match="pre-execution approval"):
            state.transition_to(ApprovalState.CLOSED)

    def test_transition_to_closed_without_closure_record_raises(self) -> None:
        """Cannot transition to CLOSED without a closure record (RA-4)."""
        state = _state_at_pending_review()
        # Has pre_approval but no closure record
        with pytest.raises(ApprovalError, match="closure record"):
            state.transition_to(ApprovalState.CLOSED)

    def test_transition_to_closed_with_both_present_succeeds(self) -> None:
        """With pre_approval and closure_record, transition to CLOSED succeeds."""
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        state.transition_to(ApprovalState.CLOSED)
        assert state.state == ApprovalState.CLOSED
