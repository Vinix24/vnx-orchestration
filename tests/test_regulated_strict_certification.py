#!/usr/bin/env python3
"""PR-4 certification tests for Feature 21: Regulated-Strict Governance."""

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
    DispatchApprovalState,
    EmptyRationaleError,
    RegulatedStrictApprovalPolicy,
    VALID_APPROVERS,
    regulated_strict_policy,
)
from audit_bundle import EmptyBundleError
from audit_bundle import (
    AuditBundle,
    AuditBundleBuilder,
    EvidenceType,
    audit_bundle_builder,
)
from regulated_strict_dashboard import (
    RegulatedStrictStatus,
    format_status_line,
    regulated_strict_surface,
)


def _policy() -> RegulatedStrictApprovalPolicy:
    return regulated_strict_policy()

def _approval(dispatch_id: str = "d-cert"):
    return _policy().record_approval(
        dispatch_id=dispatch_id, approved_by="operator",
        rationale="Reviewed", approval_type=ApprovalType.PRE_EXECUTION)

def _closure(dispatch_id: str = "d-cert"):
    return _policy().record_closure(
        dispatch_id=dispatch_id, closed_by="operator",
        rationale="Reviewed and accepted")

def _gate_result(dispatch_id: str = "d-cert"):
    return {"gate_id": "g-1", "outcome": "pass", "dispatch_id": dispatch_id,
            "timestamp": "2026-04-03T15:00:00+00:00"}

def _receipt(dispatch_id: str = "d-cert"):
    return {"receipt_id": "r-1", "dispatch_id": dispatch_id,
            "timestamp": "2026-04-03T15:01:00+00:00"}

def _complete_builder(dispatch_id: str = "d-cert") -> AuditBundleBuilder:
    builder = audit_bundle_builder(dispatch_id)
    builder.add_approval(_approval(dispatch_id))
    builder.add_closure(_closure(dispatch_id))
    builder.add_gate_result(_gate_result(dispatch_id))
    builder.add_receipt(_receipt(dispatch_id))
    return builder

def _state_at_pending_review(dispatch_id: str = "d-cert") -> DispatchApprovalState:
    state = DispatchApprovalState(dispatch_id=dispatch_id)
    state.add_pre_approval(_approval(dispatch_id))
    state.transition_to(ApprovalState.APPROVED)
    state.transition_to(ApprovalState.EXECUTING)
    state.transition_to(ApprovalState.PENDING_REVIEW)
    return state


class TestApprovalWorkflow:

    def test_initial_state_is_pending(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        assert state.state == ApprovalState.PENDING_APPROVAL

    def test_approval_transitions_to_approved(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        state.add_pre_approval(_approval())
        state.transition_to(ApprovalState.APPROVED)
        assert state.state == ApprovalState.APPROVED

    def test_empty_rationale_rejected(self) -> None:
        with pytest.raises(EmptyRationaleError):
            _policy().record_approval(
                dispatch_id="d-cert", approved_by="operator",
                rationale="", approval_type=ApprovalType.PRE_EXECUTION)

    def test_automated_approver_rejected(self) -> None:
        with pytest.raises(AutomatedApprovalError):
            _policy().record_approval(
                dispatch_id="d-cert", approved_by="automated",
                rationale="auto", approval_type=ApprovalType.PRE_EXECUTION)

    def test_approval_record_immutable(self) -> None:
        record = _approval()
        with pytest.raises(AttributeError):
            record.rationale = "changed"  # type: ignore[misc]

    def test_closure_requires_pre_approval(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        with pytest.raises(ApprovalError):
            state.transition_to(ApprovalState.APPROVED)

    def test_full_lifecycle_to_closed(self) -> None:
        state = _state_at_pending_review()
        state.apply_closure(_closure())
        state.transition_to(ApprovalState.CLOSED)
        assert state.state == ApprovalState.CLOSED

    def test_valid_approvers(self) -> None:
        assert "operator" in VALID_APPROVERS
        assert "T0" in VALID_APPROVERS


class TestAuditBundleIntegrity:

    def test_empty_bundle_rejected(self) -> None:
        builder = audit_bundle_builder("d-cert")
        with pytest.raises(EmptyBundleError):
            builder.build()

    def test_complete_bundle_builds(self) -> None:
        bundle = _complete_builder().build()
        assert isinstance(bundle, AuditBundle)

    def test_bundle_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        with pytest.raises(AttributeError):
            bundle.dispatch_id = "hacked"  # type: ignore[misc]

    def test_bundle_id_format(self) -> None:
        bundle = _complete_builder().build()
        assert bundle.bundle_id.startswith("bundle-")

    def test_cross_dispatch_rejected(self) -> None:
        builder = audit_bundle_builder("d-cert")
        wrong_approval = _approval("d-other")
        with pytest.raises(ValueError):
            builder.add_approval(wrong_approval)

    def test_five_evidence_types(self) -> None:
        assert len(list(EvidenceType)) == 5


class TestDashboardVisibility:

    def test_status_surface_created(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        policy = _policy()
        status = regulated_strict_surface("d-cert", state, policy)
        assert isinstance(status, RegulatedStrictStatus)

    def test_status_line_format(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        policy = _policy()
        status = regulated_strict_surface("d-cert", state, policy)
        line = format_status_line(status)
        assert isinstance(line, str)
        assert len(line) > 0

    def test_profile_locked(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        policy = _policy()
        status = regulated_strict_surface("d-cert", state, policy)
        assert status.profile_locked is True

    def test_to_dict(self) -> None:
        state = DispatchApprovalState(dispatch_id="d-cert")
        policy = _policy()
        status = regulated_strict_surface("d-cert", state, policy)
        d = status.to_dict()
        assert isinstance(d, dict)
        assert "dispatch_id" in d


class TestContractAlignment:

    def test_seven_approval_states(self) -> None:
        assert len(list(ApprovalState)) == 7

    def test_closure_record_immutable(self) -> None:
        record = _closure()
        with pytest.raises(AttributeError):
            record.rationale = "changed"  # type: ignore[misc]

    def test_closure_empty_rationale_rejected(self) -> None:
        with pytest.raises(EmptyRationaleError):
            _policy().record_closure(
                dispatch_id="d-cert", closed_by="operator", rationale="")
