#!/usr/bin/env python3
"""Regulated-strict dashboard surface for operator visibility (Feature 21, PR-3).

Exposes a read-only snapshot of regulated_strict governance state for a single
dispatch. This module surfaces approval state, bundle readiness, and closure
eligibility in a single inspectable structure without mutating any underlying
state.

Components:
  RegulatedStrictStatus           — frozen snapshot of regulated_strict state
  regulated_strict_surface()      — factory creating a snapshot from live objects
  assert_profile_not_downgraded() — guard against implicit profile mixing
  format_status_line()            — single-line human-readable operator summary

Design constraints:
  - Read-only: this module NEVER mutates approval state or bundle.
  - Profile-locked: governance_profile is always "regulated_strict".
  - Cross-dispatch isolation: bundle.dispatch_id must match dispatch_id.
  - No new dependencies: stdlib only (plus project internal imports).
  - Must not import from business_light_policy or governance_profile_selector.

Usage:
    from regulated_strict_approval import (
        DispatchApprovalState, regulated_strict_policy,
    )
    from audit_bundle import audit_bundle_builder

    state = DispatchApprovalState(dispatch_id="d-001")
    policy = regulated_strict_policy()
    bundle = None  # or a sealed AuditBundle

    status = regulated_strict_surface("d-001", state, policy, bundle=bundle)
    print(format_status_line(status))
    # [regulated_strict] d-001 | approval=pending_approval | bundle=not_ready | can_close=False
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from regulated_strict_approval import (
    DispatchApprovalState,
    RegulatedStrictApprovalPolicy,
)

# AuditBundle is imported for type annotation only; the import is optional
# so that this module can be loaded without audit_bundle being present.
try:
    from audit_bundle import AuditBundle
except ImportError:  # pragma: no cover
    AuditBundle = None  # type: ignore[assignment,misc]

_PROFILE = "regulated_strict"


# ---------------------------------------------------------------------------
# Profile guard
# ---------------------------------------------------------------------------

def assert_profile_not_downgraded(profile: str) -> None:
    """Raise ValueError if profile is not 'regulated_strict'.

    Used to prevent implicit profile mixing: any caller that silently passes a
    different profile string will be caught here before it reaches the surface
    logic.

    Args:
        profile: The governance profile string to validate.

    Raises:
        ValueError: If profile is not exactly "regulated_strict".
    """
    if profile != _PROFILE:
        raise ValueError(
            f"Profile downgrade detected: expected {_PROFILE!r}, got {profile!r}. "
            "Implicit profile mixing is not permitted in regulated_strict governance."
        )


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegulatedStrictStatus:
    """Frozen snapshot of regulated_strict governance state for one dispatch.

    All fields are set at construction time and cannot be mutated. The snapshot
    reflects the approval state machine and bundle readiness at a single point
    in time.

    Attributes:
        dispatch_id:       The dispatch this snapshot covers.
        governance_profile: Always "regulated_strict". Never set to another value.
        approval_state:    Current state value from DispatchApprovalState.
        has_pre_approval:  True if at least one pre-execution approval is recorded.
        has_closure_record: True if a post-review closure record is recorded.
        pre_approval_count: Number of pre-execution approvals recorded.
        bundle_ready:      True if an AuditBundle exists and is_complete() is True.
        bundle_id:         bundle_id if bundle exists, else None.
        can_close:         Result of policy.can_close(state).
        profile_locked:    Always True for regulated_strict (prevents silent downgrade).
    """
    dispatch_id:        str
    governance_profile: str
    approval_state:     str
    has_pre_approval:   bool
    has_closure_record: bool
    pre_approval_count: int
    bundle_ready:       bool
    bundle_id:          Optional[str]
    can_close:          bool
    profile_locked:     bool

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of this status snapshot.

        All values are primitive types. The result can be passed directly to
        json.dumps() without further transformation.

        Returns:
            Dict with all fields as JSON-serializable values.
        """
        return {
            "dispatch_id":        self.dispatch_id,
            "governance_profile": self.governance_profile,
            "approval_state":     self.approval_state,
            "has_pre_approval":   self.has_pre_approval,
            "has_closure_record": self.has_closure_record,
            "pre_approval_count": self.pre_approval_count,
            "bundle_ready":       self.bundle_ready,
            "bundle_id":          self.bundle_id,
            "can_close":          self.can_close,
            "profile_locked":     self.profile_locked,
        }


# ---------------------------------------------------------------------------
# Surface factory
# ---------------------------------------------------------------------------

def regulated_strict_surface(
    dispatch_id: str,
    approval_state: DispatchApprovalState,
    policy: RegulatedStrictApprovalPolicy,
    *,
    bundle: Optional[object] = None,
) -> RegulatedStrictStatus:
    """Create a RegulatedStrictStatus snapshot from live governed objects.

    This function is read-only: it inspects the provided objects and returns
    a frozen snapshot. It never mutates approval_state or bundle.

    Args:
        dispatch_id:    The dispatch ID this snapshot is for.
        approval_state: Live DispatchApprovalState for the dispatch.
        policy:         Live RegulatedStrictApprovalPolicy instance.
        bundle:         Optional AuditBundle. If provided, its dispatch_id
                        must match dispatch_id.

    Returns:
        Frozen RegulatedStrictStatus snapshot.

    Raises:
        ValueError: If bundle.dispatch_id does not match dispatch_id.
        ValueError: If approval_state.dispatch_id does not match dispatch_id.
    """
    if approval_state.dispatch_id != dispatch_id:
        raise ValueError(
            f"approval_state.dispatch_id {approval_state.dispatch_id!r} does not match "
            f"dispatch_id {dispatch_id!r}. Cross-dispatch surface is not permitted."
        )

    bundle_ready = False
    bundle_id: Optional[str] = None

    if bundle is not None:
        # Enforce cross-dispatch isolation.
        if getattr(bundle, "dispatch_id", None) != dispatch_id:
            raise ValueError(
                f"bundle.dispatch_id {getattr(bundle, 'dispatch_id', None)!r} does not match "
                f"dispatch_id {dispatch_id!r}. Cross-dispatch bundle is not permitted."
            )
        bundle_id = getattr(bundle, "bundle_id", None)
        is_complete_fn = getattr(bundle, "is_complete", None)
        bundle_ready = bool(is_complete_fn()) if callable(is_complete_fn) else False

    return RegulatedStrictStatus(
        dispatch_id=dispatch_id,
        governance_profile=_PROFILE,
        approval_state=approval_state.state.value,
        has_pre_approval=approval_state.has_pre_execution_approval(),
        has_closure_record=approval_state.has_closure_record(),
        pre_approval_count=len(approval_state.pre_approvals),
        bundle_ready=bundle_ready,
        bundle_id=bundle_id,
        can_close=policy.can_close(approval_state),
        profile_locked=True,
    )


# ---------------------------------------------------------------------------
# Operator display
# ---------------------------------------------------------------------------

def format_status_line(status: RegulatedStrictStatus) -> str:
    """Return a single-line human-readable summary for operator display.

    Format:
        [regulated_strict] <dispatch_id> | approval=<state> | bundle=<ready|not_ready> | can_close=<True|False>

    Args:
        status: A RegulatedStrictStatus snapshot.

    Returns:
        Single-line string suitable for terminal output or log entries.
    """
    bundle_label = "ready" if status.bundle_ready else "not_ready"
    return (
        f"[{status.governance_profile}] {status.dispatch_id}"
        f" | approval={status.approval_state}"
        f" | bundle={bundle_label}"
        f" | can_close={status.can_close}"
    )
