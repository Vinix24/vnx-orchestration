"""test_dispatch_internal.py — Tests for dispatch_internal permit system.

Verifies that:
- issue_permit() mints a valid permit for a plan
- require_permit() passes for a legitimately minted permit
- require_permit() raises PermissionError for hand-constructed permits (missing sentinel)
- require_permit() raises PermissionError for mismatched plan_digest
- require_permit() raises PermissionError for mismatched dispatch_id
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_internal import (  # noqa: E402
    ExecutionPermit,
    PlanLike,
    issue_permit,
    require_permit,
)


# ---------------------------------------------------------------------------
# Fake plan — satisfies PlanLike
# ---------------------------------------------------------------------------

class FakePlan:
    def __init__(self, dispatch_id: str, digest_value: str = "abc123"):
        self._dispatch_id = dispatch_id
        self._digest_value = digest_value

    @property
    def dispatch_id(self) -> str:
        return self._dispatch_id

    def digest(self) -> str:
        return self._digest_value


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestIssuedPermitPasses:
    def test_require_permit_passes_for_issued_permit(self):
        plan = FakePlan("dispatch-001")
        permit = issue_permit(plan)
        require_permit(plan, permit)  # must not raise

    def test_issued_permit_has_correct_fields(self):
        plan = FakePlan("dispatch-002", "sha256xyz")
        permit = issue_permit(plan)
        assert permit.dispatch_id == "dispatch-002"
        assert permit.plan_digest == "sha256xyz"


# ---------------------------------------------------------------------------
# Forgery via hand-construction (wrong/missing sentinel)
# ---------------------------------------------------------------------------

class TestHandConstructedPermitRejected:
    def test_rejects_hand_constructed_permit_with_wrong_sentinel(self):
        """A permit built with an arbitrary sentinel object is rejected."""
        plan = FakePlan("dispatch-003")
        fake_sentinel = object()
        forged = ExecutionPermit(
            dispatch_id="dispatch-003",
            plan_digest=plan.digest(),
            _sentinel=fake_sentinel,
        )
        with pytest.raises(PermissionError):
            require_permit(plan, forged)

    def test_rejects_hand_constructed_permit_with_default_sentinel_slot(self):
        """Direct construction without _sentinel kwarg must be rejected.

        The _sentinel field defaults to None (not the real sentinel), so any permit
        built via the public constructor without explicit _sentinel access is rejected.
        Only issue_permit() attaches _PERMIT_SENTINEL — that is the un-evadable backstop.
        """
        plan = FakePlan("dispatch-004")
        permit = ExecutionPermit(
            dispatch_id=plan.dispatch_id,
            plan_digest=plan.digest(),
        )
        with pytest.raises(PermissionError):
            require_permit(plan, permit)

    def test_bare_constructed_permit_is_rejected(self):
        """An ExecutionPermit built via the public constructor (no access to the module
        sentinel) MUST be rejected — this is the un-evadable backstop's whole point."""
        plan = FakePlan("dispatch-004b", "digest-bare")
        forged = ExecutionPermit(plan.dispatch_id, plan.digest())  # no _sentinel passed
        with pytest.raises(PermissionError):
            require_permit(plan, forged)


# ---------------------------------------------------------------------------
# Mismatched plan_digest
# ---------------------------------------------------------------------------

class TestMismatchedDigestRejected:
    def test_rejects_permit_with_wrong_digest(self):
        plan_a = FakePlan("dispatch-005", digest_value="digest-a")
        plan_b = FakePlan("dispatch-005", digest_value="digest-b")
        permit = issue_permit(plan_a)
        with pytest.raises(PermissionError):
            require_permit(plan_b, permit)

    def test_rejects_permit_for_tampered_plan(self):
        """Simulate a plan whose digest changes after permit issuance."""
        plan = FakePlan("dispatch-006", digest_value="original")
        permit = issue_permit(plan)
        # Tamper: change the digest the plan returns
        plan._digest_value = "tampered"
        with pytest.raises(PermissionError):
            require_permit(plan, permit)


# ---------------------------------------------------------------------------
# Mismatched dispatch_id
# ---------------------------------------------------------------------------

class TestMismatchedDispatchIdRejected:
    def test_rejects_permit_for_different_dispatch_id(self):
        plan_a = FakePlan("dispatch-007")
        plan_b = FakePlan("dispatch-999")
        permit = issue_permit(plan_a)
        with pytest.raises(PermissionError):
            require_permit(plan_b, permit)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestPlanLikeProtocol:
    def test_fake_plan_satisfies_planlike(self):
        plan = FakePlan("x")
        assert isinstance(plan, PlanLike)
