#!/usr/bin/env python3
"""Tests for regulated_strict dashboard surface (Feature 21, PR-3).

Covers:
  1.  RegulatedStrictStatus.governance_profile — always "regulated_strict"
  2.  RegulatedStrictStatus.profile_locked     — always True
  3.  assert_profile_not_downgraded            — rejects any non-regulated_strict profile
  4.  regulated_strict_surface                 — reflects has_pre_approval,
                                                 has_closure_record, can_close
  5.  bundle_ready                             — True only when bundle exists AND is_complete()
  6.  Cross-dispatch bundle rejection          — ValueError on dispatch_id mismatch
  7.  format_status_line                       — contains dispatch_id, approval state,
                                                 bundle readiness, can_close
  8.  to_dict()                                — JSON-serializable, all fields present
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from regulated_strict_approval import (
    ApprovalState,
    ApprovalType,
    DispatchApprovalState,
    regulated_strict_policy,
)
from regulated_strict_dashboard import (
    RegulatedStrictStatus,
    assert_profile_not_downgraded,
    format_status_line,
    regulated_strict_surface,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(dispatch_id: str = "d-001") -> DispatchApprovalState:
    """Return a fresh DispatchApprovalState in PENDING_APPROVAL."""
    return DispatchApprovalState(dispatch_id=dispatch_id)


def _make_state_with_pre_approval(dispatch_id: str = "d-001") -> DispatchApprovalState:
    """Return a DispatchApprovalState with one pre-execution approval."""
    policy = regulated_strict_policy()
    state = _make_state(dispatch_id)
    record = policy.record_approval(
        dispatch_id=dispatch_id,
        approved_by="operator",
        rationale="Approved for test.",
        approval_type=ApprovalType.PRE_EXECUTION,
    )
    state.add_pre_approval(record)
    return state


def _make_bundle_mock(dispatch_id: str = "d-001", complete: bool = True) -> MagicMock:
    """Return a mock AuditBundle with the given dispatch_id and completeness."""
    bundle = MagicMock()
    bundle.dispatch_id = dispatch_id
    bundle.bundle_id = f"bundle-test-{dispatch_id}"
    bundle.is_complete.return_value = complete
    return bundle


# ---------------------------------------------------------------------------
# 1. governance_profile is always "regulated_strict"
# ---------------------------------------------------------------------------

class TestGovernanceProfileFixed:
    def test_profile_is_regulated_strict(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.governance_profile == "regulated_strict"

    def test_profile_is_regulated_strict_with_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock()
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.governance_profile == "regulated_strict"

    def test_profile_cannot_be_changed_frozen(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        with pytest.raises((AttributeError, TypeError)):
            status.governance_profile = "business_light"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. profile_locked is always True
# ---------------------------------------------------------------------------

class TestProfileLockedAlwaysTrue:
    def test_profile_locked_true_without_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.profile_locked is True

    def test_profile_locked_true_with_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock()
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.profile_locked is True

    def test_profile_locked_immutable(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        with pytest.raises((AttributeError, TypeError)):
            status.profile_locked = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. assert_profile_not_downgraded
# ---------------------------------------------------------------------------

class TestAssertProfileNotDowngraded:
    def test_accepts_regulated_strict(self):
        # Must not raise
        assert_profile_not_downgraded("regulated_strict")

    def test_rejects_business_light(self):
        with pytest.raises(ValueError, match="business_light"):
            assert_profile_not_downgraded("business_light")

    def test_rejects_coding_strict(self):
        with pytest.raises(ValueError, match="coding_strict"):
            assert_profile_not_downgraded("coding_strict")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            assert_profile_not_downgraded("")

    def test_rejects_none_like(self):
        with pytest.raises(ValueError):
            assert_profile_not_downgraded("none")

    def test_rejects_arbitrary_string(self):
        with pytest.raises(ValueError, match="regulated_strict"):
            assert_profile_not_downgraded("custom_profile")

    def test_error_message_mentions_expected_profile(self):
        with pytest.raises(ValueError) as exc_info:
            assert_profile_not_downgraded("business_light")
        assert "regulated_strict" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. regulated_strict_surface reflects approval state fields
# ---------------------------------------------------------------------------

class TestSurfaceApprovalFields:
    def test_has_pre_approval_false_when_no_approval(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.has_pre_approval is False
        assert status.pre_approval_count == 0

    def test_has_pre_approval_true_after_approval(self):
        state = _make_state_with_pre_approval()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.has_pre_approval is True
        assert status.pre_approval_count == 1

    def test_pre_approval_count_multiple(self):
        policy = regulated_strict_policy()
        state = _make_state()
        for i in range(3):
            rec = policy.record_approval(
                dispatch_id="d-001",
                approved_by="operator",
                rationale=f"Approval {i}.",
            )
            state.add_pre_approval(rec)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.pre_approval_count == 3

    def test_has_closure_record_false_initially(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.has_closure_record is False

    def test_has_closure_record_true_after_closure(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        closure = policy.record_closure(
            dispatch_id="d-001",
            closed_by="operator",
            rationale="Closure rationale.",
        )
        state.apply_closure(closure)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.has_closure_record is True

    def test_approval_state_reflects_current_state(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.approval_state == "pending_approval"

    def test_approval_state_after_transition(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        state.transition_to(ApprovalState.APPROVED)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.approval_state == "approved"

    def test_dispatch_id_in_status(self):
        state = _make_state("dispatch-xyz")
        policy = regulated_strict_policy()
        status = regulated_strict_surface("dispatch-xyz", state, policy)
        assert status.dispatch_id == "dispatch-xyz"

    def test_cross_dispatch_state_raises(self):
        state = _make_state("d-999")
        policy = regulated_strict_policy()
        with pytest.raises(ValueError, match="d-999"):
            regulated_strict_surface("d-001", state, policy)


# ---------------------------------------------------------------------------
# 5. can_close reflects policy.can_close
# ---------------------------------------------------------------------------

class TestCanClose:
    def test_can_close_false_no_approval(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.can_close is False

    def test_can_close_false_no_closure_record(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.can_close is False

    def test_can_close_true_with_all_requirements(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        closure = policy.record_closure(
            dispatch_id="d-001",
            closed_by="operator",
            rationale="All requirements met.",
        )
        state.apply_closure(closure)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.can_close is True

    def test_can_close_matches_policy_directly(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        direct_result = policy.can_close(state)
        status = regulated_strict_surface("d-001", state, policy)
        assert status.can_close == direct_result


# ---------------------------------------------------------------------------
# 6. bundle_ready — True only when bundle exists AND is_complete() is True
# ---------------------------------------------------------------------------

class TestBundleReady:
    def test_bundle_ready_false_when_no_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy, bundle=None)
        assert status.bundle_ready is False
        assert status.bundle_id is None

    def test_bundle_ready_true_when_complete(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(complete=True)
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.bundle_ready is True
        assert status.bundle_id == bundle.bundle_id

    def test_bundle_ready_false_when_incomplete(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(complete=False)
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.bundle_ready is False
        assert status.bundle_id == bundle.bundle_id

    def test_bundle_id_is_none_when_no_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.bundle_id is None

    def test_bundle_id_present_when_bundle_provided(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(complete=False)
        bundle.bundle_id = "bundle-abc-123"
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.bundle_id == "bundle-abc-123"


# ---------------------------------------------------------------------------
# 7. Cross-dispatch bundle rejection
# ---------------------------------------------------------------------------

class TestCrossDispatchBundleRejection:
    def test_raises_on_bundle_dispatch_id_mismatch(self):
        state = _make_state("d-001")
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(dispatch_id="d-999")
        with pytest.raises(ValueError, match="d-999"):
            regulated_strict_surface("d-001", state, policy, bundle=bundle)

    def test_error_message_mentions_both_ids(self):
        state = _make_state("d-001")
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(dispatch_id="d-999")
        with pytest.raises(ValueError) as exc_info:
            regulated_strict_surface("d-001", state, policy, bundle=bundle)
        msg = str(exc_info.value)
        assert "d-999" in msg or "d-001" in msg

    def test_matching_dispatch_id_does_not_raise(self):
        state = _make_state("d-001")
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(dispatch_id="d-001")
        # Should not raise
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.dispatch_id == "d-001"


# ---------------------------------------------------------------------------
# 8. format_status_line
# ---------------------------------------------------------------------------

class TestFormatStatusLine:
    def test_contains_dispatch_id(self):
        state = _make_state("d-42")
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-42", state, policy)
        line = format_status_line(status)
        assert "d-42" in line

    def test_contains_approval_state(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "pending_approval" in line

    def test_contains_bundle_not_ready(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "not_ready" in line

    def test_contains_bundle_ready(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(complete=True)
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        line = format_status_line(status)
        assert "ready" in line
        assert "not_ready" not in line

    def test_contains_can_close(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "can_close" in line

    def test_can_close_true_in_line(self):
        policy = regulated_strict_policy()
        state = _make_state_with_pre_approval()
        state.transition_to(ApprovalState.APPROVED)
        state.transition_to(ApprovalState.EXECUTING)
        state.transition_to(ApprovalState.PENDING_REVIEW)
        closure = policy.record_closure(
            dispatch_id="d-001",
            closed_by="operator",
            rationale="Closure.",
        )
        state.apply_closure(closure)
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "can_close=True" in line

    def test_can_close_false_in_line(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "can_close=False" in line

    def test_line_contains_regulated_strict_prefix(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "[regulated_strict]" in line

    def test_is_single_line(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        line = format_status_line(status)
        assert "\n" not in line


# ---------------------------------------------------------------------------
# 9. to_dict() — JSON-serializable, all fields present
# ---------------------------------------------------------------------------

class TestToDict:
    def test_to_dict_is_json_serializable(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        d = status.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_to_dict_contains_all_required_fields(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        d = status.to_dict()
        required_fields = {
            "dispatch_id",
            "governance_profile",
            "approval_state",
            "has_pre_approval",
            "has_closure_record",
            "pre_approval_count",
            "bundle_ready",
            "bundle_id",
            "can_close",
            "profile_locked",
        }
        assert required_fields <= set(d.keys())

    def test_to_dict_governance_profile_is_regulated_strict(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.to_dict()["governance_profile"] == "regulated_strict"

    def test_to_dict_profile_locked_is_true(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.to_dict()["profile_locked"] is True

    def test_to_dict_bundle_id_none_when_no_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        status = regulated_strict_surface("d-001", state, policy)
        assert status.to_dict()["bundle_id"] is None

    def test_to_dict_bundle_id_present_when_bundle(self):
        state = _make_state()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock()
        bundle.bundle_id = "bundle-xyz"
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        assert status.to_dict()["bundle_id"] == "bundle-xyz"

    def test_to_dict_roundtrip(self):
        state = _make_state_with_pre_approval()
        policy = regulated_strict_policy()
        bundle = _make_bundle_mock(complete=True)
        status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
        d = status.to_dict()
        # Verify round-trip integrity: values match original status fields
        assert d["dispatch_id"] == status.dispatch_id
        assert d["has_pre_approval"] == status.has_pre_approval
        assert d["bundle_ready"] == status.bundle_ready
        assert d["can_close"] == status.can_close
