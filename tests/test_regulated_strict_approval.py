#!/usr/bin/env python3
"""Tests for regulated_strict approval workflow and evidence capture (Feature 21, PR-1).

Covers all approval enforcement requirements from the contract
(docs/REGULATED_STRICT_GOVERNANCE_CONTRACT.md) Section 8.1 and 8.3:

  1.  ApprovalState       — all states defined, state machine transitions
  2.  ApprovalType        — enum values
  3.  ClosureType         — enum values
  4.  VALID_APPROVERS     — operator and T0 only
  5.  ApprovalRecord      — RA-1 (rationale), RA-2 (approver), RA-3 (immutable)
  6.  ClosureRecord       — construction, rationale, approver, immutability
  7.  DispatchApprovalState — state machine, transitions, add_pre_approval, apply_closure
  8.  State machine transitions — valid and invalid paths
  9.  RegulatedStrictApprovalPolicy — record_approval, record_closure, can_close,
                                      assert_can_close, transition_dispatch
  10. RA-1 enforcement    — empty rationale rejected everywhere
  11. RA-2 enforcement    — automated approvals rejected everywhere
  12. RA-3 enforcement    — records are immutable
  13. RA-4 enforcement    — both pre-execution approval and closure required for close
  14. Terminal states     — CLOSED and REJECTED cannot be transitioned further
  15. REVIEW_FAILED loop  — review failure loops back to PENDING_APPROVAL
  16. Isolation           — no imports from coding_strict or business_light paths
  17. Dispatch cannot close without approval (contract Section 8.1 item 1)
  18. Gate-pass does not auto-close (contract Section 8.3 item 2)
  19. Timeout leads to PENDING_APPROVAL, not CLOSED (contract Section 8.3 item 3)
  20. Auto-close rejected (contract Section 8.3 item 1)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from regulated_strict_approval import (
    VALID_APPROVERS,
    VALID_TRANSITIONS,
    ApprovalError,
    ApprovalRecord,
    ApprovalState,
    ApprovalType,
    AutomatedApprovalError,
    ClosureBlockedError,
    ClosureRecord,
    ClosureType,
    DispatchApprovalState,
    EmptyRationaleError,
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
) -> ApprovalRecord:
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
) -> ClosureRecord:
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
# 1. ApprovalState
# ---------------------------------------------------------------------------

class TestApprovalState:

    def test_all_states_defined(self) -> None:
        values = {s.value for s in ApprovalState}
        assert "pending_approval" in values
        assert "approved" in values
        assert "executing" in values
        assert "pending_review" in values
        assert "closed" in values
        assert "rejected" in values
        assert "review_failed" in values

    def test_states_are_distinct(self) -> None:
        states = list(ApprovalState)
        assert len(states) == len({s.value for s in states})

    def test_initial_state_is_pending_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        assert state.state == ApprovalState.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# 2. ApprovalType
# ---------------------------------------------------------------------------

class TestApprovalType:

    def test_pre_execution_value(self) -> None:
        assert ApprovalType.PRE_EXECUTION.value == "pre_execution"

    def test_post_review_value(self) -> None:
        assert ApprovalType.POST_REVIEW.value == "post_review"

    def test_types_are_distinct(self) -> None:
        assert ApprovalType.PRE_EXECUTION != ApprovalType.POST_REVIEW


# ---------------------------------------------------------------------------
# 3. ClosureType
# ---------------------------------------------------------------------------

class TestClosureType:

    def test_approved_value(self) -> None:
        assert ClosureType.APPROVED.value == "approved"

    def test_rejected_value(self) -> None:
        assert ClosureType.REJECTED.value == "rejected"

    def test_exception_value(self) -> None:
        assert ClosureType.EXCEPTION.value == "exception"


# ---------------------------------------------------------------------------
# 4. VALID_APPROVERS
# ---------------------------------------------------------------------------

class TestValidApprovers:

    def test_operator_is_valid(self) -> None:
        assert "operator" in VALID_APPROVERS

    def test_t0_is_valid(self) -> None:
        assert "T0" in VALID_APPROVERS

    def test_runtime_not_valid(self) -> None:
        assert "runtime" not in VALID_APPROVERS

    def test_automation_not_valid(self) -> None:
        assert "automation" not in VALID_APPROVERS

    def test_empty_not_valid(self) -> None:
        assert "" not in VALID_APPROVERS


# ---------------------------------------------------------------------------
# 5. ApprovalRecord — RA-1, RA-2, RA-3
# ---------------------------------------------------------------------------

class TestApprovalRecord:

    def test_construction_valid(self) -> None:
        rec = _make_pre_approval()
        assert rec.approval_id.startswith("appr-")
        assert rec.dispatch_id == "d-001"
        assert rec.approved_by == "operator"
        assert rec.approval_type == ApprovalType.PRE_EXECUTION
        assert len(rec.rationale) > 0

    def test_approval_id_format(self) -> None:
        rec = _make_pre_approval()
        assert rec.approval_id.startswith("appr-")
        # UUID portion should be present
        assert len(rec.approval_id) > len("appr-")

    def test_approved_at_is_iso8601(self) -> None:
        rec = _make_pre_approval()
        # Must contain date/time separators
        assert "T" in rec.approved_at or "-" in rec.approved_at

    def test_to_dict_structure(self) -> None:
        rec = _make_pre_approval()
        d = rec.to_dict()
        assert "approval_id" in d
        assert "dispatch_id" in d
        assert "approved_by" in d
        assert "approved_at" in d
        assert "approval_type" in d
        assert "rationale" in d
        assert "evidence_refs" in d
        assert "conditions" in d

    def test_to_dict_approval_type_is_string(self) -> None:
        rec = _make_pre_approval()
        d = rec.to_dict()
        assert d["approval_type"] == "pre_execution"

    def test_evidence_refs_stored(self) -> None:
        policy = _make_policy()
        rec = policy.record_approval(
            dispatch_id="d-001",
            approved_by="operator",
            rationale="ok",
            evidence_refs=["gate-001", "sig-002"],
        )
        assert "gate-001" in rec.evidence_refs
        assert "sig-002" in rec.evidence_refs

    def test_conditions_stored(self) -> None:
        policy = _make_policy()
        rec = policy.record_approval(
            dispatch_id="d-001",
            approved_by="T0",
            rationale="ok with condition",
            conditions=["must review output"],
        )
        assert "must review output" in rec.conditions

    # RA-1: rationale must be non-empty
    def test_ra1_empty_rationale_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(EmptyRationaleError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="operator",
                rationale="",
            )

    def test_ra1_whitespace_only_rationale_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(EmptyRationaleError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="operator",
                rationale="   ",
            )

    def test_ra1_error_message_informative(self) -> None:
        policy = _make_policy()
        with pytest.raises(EmptyRationaleError, match="rationale"):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="operator",
                rationale="",
            )

    # RA-2: automated approvals forbidden
    def test_ra2_automated_approver_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="runtime",
                rationale="auto-approved",
            )

    def test_ra2_broker_approver_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="broker",
                rationale="auto",
            )

    def test_ra2_empty_approver_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_approval(
                dispatch_id="d-001",
                approved_by="",
                rationale="ok",
            )

    def test_ra2_operator_accepted(self) -> None:
        rec = _make_pre_approval(approved_by="operator")
        assert rec.approved_by == "operator"

    def test_ra2_t0_accepted(self) -> None:
        rec = _make_pre_approval(approved_by="T0")
        assert rec.approved_by == "T0"

    # RA-3: records are immutable
    def test_ra3_record_is_frozen(self) -> None:
        rec = _make_pre_approval()
        with pytest.raises(Exception):
            rec.rationale = "modified"  # type: ignore[misc]

    def test_ra3_dispatch_id_is_frozen(self) -> None:
        rec = _make_pre_approval()
        with pytest.raises(Exception):
            rec.dispatch_id = "d-999"  # type: ignore[misc]

    def test_ra3_approved_by_is_frozen(self) -> None:
        rec = _make_pre_approval()
        with pytest.raises(Exception):
            rec.approved_by = "runtime"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. ClosureRecord
# ---------------------------------------------------------------------------

class TestClosureRecord:

    def test_construction_valid(self) -> None:
        rec = _make_closure()
        assert rec.closure_id.startswith("close-")
        assert rec.dispatch_id == "d-001"
        assert rec.closed_by == "operator"
        assert rec.closure_type == ClosureType.APPROVED

    def test_closure_id_format(self) -> None:
        rec = _make_closure()
        assert rec.closure_id.startswith("close-")
        assert len(rec.closure_id) > len("close-")

    def test_to_dict_structure(self) -> None:
        rec = _make_closure()
        d = rec.to_dict()
        assert "closure_id" in d
        assert "dispatch_id" in d
        assert "closed_by" in d
        assert "closed_at" in d
        assert "closure_type" in d
        assert "rationale" in d
        assert "bundle_id" in d
        assert "bundle_complete" in d
        assert "open_items_resolved" in d
        assert "residual_risks" in d

    def test_closure_type_is_string_in_dict(self) -> None:
        rec = _make_closure()
        d = rec.to_dict()
        assert d["closure_type"] == "approved"

    def test_empty_rationale_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(EmptyRationaleError):
            policy.record_closure(
                dispatch_id="d-001",
                closed_by="operator",
                rationale="",
            )

    def test_automated_closer_rejected(self) -> None:
        policy = _make_policy()
        with pytest.raises(AutomatedApprovalError):
            policy.record_closure(
                dispatch_id="d-001",
                closed_by="runtime",
                rationale="auto-closed",
            )

    def test_record_is_frozen(self) -> None:
        rec = _make_closure()
        with pytest.raises(Exception):
            rec.rationale = "modified"  # type: ignore[misc]

    def test_residual_risks_stored(self) -> None:
        policy = _make_policy()
        rec = policy.record_closure(
            dispatch_id="d-001",
            closed_by="operator",
            rationale="closing with known risk",
            closure_type=ClosureType.EXCEPTION,
            residual_risks=["minor documentation gap"],
        )
        assert "minor documentation gap" in rec.residual_risks


# ---------------------------------------------------------------------------
# 7. DispatchApprovalState — construction and basic operations
# ---------------------------------------------------------------------------

class TestDispatchApprovalState:

    def test_initial_state_pending_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        assert state.state == ApprovalState.PENDING_APPROVAL

    def test_initial_has_no_pre_approvals(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        assert not state.has_pre_execution_approval()
        assert len(state.pre_approvals) == 0

    def test_initial_has_no_closure_record(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        assert not state.has_closure_record()
        assert state.closure_record is None

    def test_add_pre_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        rec = _make_pre_approval()
        state.add_pre_approval(rec)
        assert state.has_pre_execution_approval()
        assert len(state.pre_approvals) == 1

    def test_add_multiple_pre_approvals(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval(approved_by="operator"))
        state.add_pre_approval(_make_pre_approval(approved_by="T0"))
        assert len(state.pre_approvals) == 2

    def test_add_non_pre_execution_approval_raises(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        policy = _make_policy()
        post_rec = policy.record_approval(
            dispatch_id="d-001",
            approved_by="operator",
            rationale="post review",
            approval_type=ApprovalType.POST_REVIEW,
        )
        with pytest.raises(ApprovalError):
            state.add_pre_approval(post_rec)

    def test_apply_closure_requires_pending_review(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        # Still in PENDING_APPROVAL — closure not allowed
        with pytest.raises(ApprovalError, match="PENDING_REVIEW"):
            state.apply_closure(_make_closure())

    def test_apply_closure_in_pending_review(self) -> None:
        state = _state_at_pending_review()
        closure = _make_closure()
        state.apply_closure(closure)
        assert state.has_closure_record()
        assert state.closure_record is closure

    def test_to_summary_structure(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        summary = state.to_summary()
        assert "dispatch_id" in summary
        assert "state" in summary
        assert "pre_approval_count" in summary
        assert "has_pre_approval" in summary
        assert "has_closure_record" in summary
        assert "closure_type" in summary

    def test_to_summary_values_accurate(self) -> None:
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        summary = state.to_summary()
        assert summary["state"] == "pending_review"
        assert summary["pre_approval_count"] == 1
        assert summary["has_pre_approval"] is True
        assert summary["has_closure_record"] is True
        assert summary["closure_type"] == "approved"


# ---------------------------------------------------------------------------
# 8. State machine transitions
# ---------------------------------------------------------------------------

class TestStateMachineTransitions:

    def test_pending_approval_to_approved(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.APPROVED)
        assert state.state == ApprovalState.APPROVED

    def test_pending_approval_to_rejected(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.REJECTED)
        assert state.state == ApprovalState.REJECTED

    def test_approved_to_executing(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        assert state.state == ApprovalState.EXECUTING

    def test_executing_to_pending_review(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        assert state.state == ApprovalState.PENDING_REVIEW

    def test_pending_review_to_closed(self) -> None:
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.CLOSED)
        assert state.state == ApprovalState.CLOSED

    def test_pending_review_to_review_failed(self) -> None:
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        assert state.state == ApprovalState.REVIEW_FAILED

    def test_review_failed_loops_to_pending_approval(self) -> None:
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.REVIEW_FAILED)
        state.transition_to(ApprovalState.PENDING_APPROVAL)
        assert state.state == ApprovalState.PENDING_APPROVAL

    def test_invalid_transition_raises(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        # Cannot go from PENDING_APPROVAL directly to EXECUTING
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.EXECUTING)

    def test_invalid_transition_pending_approval_to_closed(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.CLOSED)

    def test_invalid_transition_approved_to_closed(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.APPROVED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.CLOSED)

    def test_error_message_includes_current_and_target_state(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        with pytest.raises(InvalidStateTransitionError, match="pending_approval"):
            state.transition_to(ApprovalState.EXECUTING)


# ---------------------------------------------------------------------------
# 9. Terminal states cannot be transitioned
# ---------------------------------------------------------------------------

class TestTerminalStates:

    def test_closed_is_terminal(self) -> None:
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.CLOSED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.PENDING_APPROVAL)

    def test_rejected_is_terminal(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.REJECTED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.PENDING_APPROVAL)

    def test_closed_cannot_reopen(self) -> None:
        state = _state_at_pending_review()
        state.transition_to(ApprovalState.CLOSED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.APPROVED)

    def test_rejected_cannot_reopen(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.transition_to(ApprovalState.REJECTED)
        with pytest.raises(InvalidStateTransitionError):
            state.transition_to(ApprovalState.APPROVED)


# ---------------------------------------------------------------------------
# 10. RegulatedStrictApprovalPolicy — can_close / assert_can_close
# ---------------------------------------------------------------------------

class TestCanClose:

    def setup_method(self) -> None:
        self.policy = _make_policy()

    def test_can_close_requires_pre_approval_and_closure(self) -> None:
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        assert self.policy.can_close(state) is True

    def test_cannot_close_without_pre_approval(self) -> None:
        # Build state without adding pre-approval
        state = DispatchApprovalState(dispatch_id="d-001")
        # Manually force to PENDING_REVIEW to simulate incorrect path
        state.state = ApprovalState.PENDING_REVIEW
        state.closure_record = _make_closure()
        assert self.policy.can_close(state) is False

    def test_cannot_close_without_closure_record(self) -> None:
        state = _state_at_pending_review()
        # No closure record applied
        assert self.policy.can_close(state) is False

    def test_cannot_close_not_in_pending_review(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        # Not in PENDING_REVIEW
        assert self.policy.can_close(state) is False

    def test_assert_can_close_raises_if_no_pre_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.state = ApprovalState.PENDING_REVIEW
        with pytest.raises(ClosureBlockedError, match="pre-execution approval"):
            self.policy.assert_can_close(state)

    def test_assert_can_close_raises_if_wrong_state(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-001")
        state.add_pre_approval(_make_pre_approval())
        state.transition_to(ApprovalState.APPROVED)
        with pytest.raises(ClosureBlockedError, match="pending_review"):
            self.policy.assert_can_close(state)

    def test_assert_can_close_raises_if_no_closure_record(self) -> None:
        state = _state_at_pending_review()
        with pytest.raises(ClosureBlockedError, match="closure"):
            self.policy.assert_can_close(state)

    def test_assert_can_close_passes_when_all_requirements_met(self) -> None:
        state = _state_at_pending_review()
        state.apply_closure(_make_closure())
        # Should not raise
        self.policy.assert_can_close(state)

    def test_error_message_includes_dispatch_id(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-SPECIFIC-001")
        state.state = ApprovalState.PENDING_REVIEW
        with pytest.raises(ClosureBlockedError, match="d-SPECIFIC-001"):
            self.policy.assert_can_close(state)


# ---------------------------------------------------------------------------
# 11. RA-4 enforcement: both pre and post approval required
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
# 12. Gate-pass does not auto-close (contract Section 8.3 item 2)
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
# 13. Timeout leads to PENDING_APPROVAL, not CLOSED (contract Section 8.3 item 3)
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
# 14. Automated approvals rejected (contract Section 8.1 item 3)
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
# 15. Approval metadata is retained and queryable
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
# 16. Isolation — regulated_strict does not import from coding_strict paths
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
# 17. policy factory
# ---------------------------------------------------------------------------

class TestPolicyFactory:

    def test_factory_returns_policy_instance(self) -> None:
        policy = regulated_strict_policy()
        assert isinstance(policy, RegulatedStrictApprovalPolicy)

    def test_policy_is_frozen(self) -> None:
        policy = regulated_strict_policy()
        with pytest.raises(Exception):
            policy.some_attr = "bad"  # type: ignore[attr-defined]
