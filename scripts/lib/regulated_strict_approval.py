#!/usr/bin/env python3
"""Regulated-strict approval workflow and evidence capture (Feature 21, PR-1).

Implements the explicit approval state machine and immutable approval records
required by the `regulated_strict` governance profile per the contract in
`docs/REGULATED_STRICT_GOVERNANCE_CONTRACT.md`.

Components:
  ApprovalState           — dispatch lifecycle states for regulated_strict
  ApprovalType            — pre_execution | post_review
  ClosureType             — approved | rejected | exception
  ApprovedBy              — valid approver identities (operator | T0)
  ApprovalRecord          — immutable approval record with RA-1..RA-4 invariants
  ClosureRecord           — explicit post-review closure record
  DispatchApprovalState   — mutable approval state machine for one dispatch
  RegulatedStrictApprovalPolicy — enforcement engine for approval workflow
  regulated_strict_policy()    — factory returning the canonical policy instance

Design invariants (per contract Section 2.3):
  - RA-1: rationale must be non-empty. Empty-string approvals are rejected.
  - RA-2: approved_by must be "operator" or "T0". Automated approvals are forbidden.
  - RA-3: ApprovalRecord is immutable after creation. No amendments.
  - RA-4: Every dispatch requires at least one pre-execution approval AND one
          post-review closure before it can close.

State machine (per contract Section 2.2):
  PENDING_APPROVAL -> APPROVED -> EXECUTING -> PENDING_REVIEW -> CLOSED
       |                                              |
       v                                              v
  REJECTED (terminal)                     REVIEW_FAILED -> PENDING_APPROVAL (loop)

Cross-profile isolation (per contract Section 6):
  - coding_strict and business_light operations are unaware of regulated_strict state.
  - This module is only activated for regulated_strict dispatches.
  - No side-effects on non-regulated governance paths.

Feature flag: VNX_REGULATED_STRICT_ENABLED
  0 (default) = profile defined but not activatable (audit_gate missing)
  1 = pilot mode — explicit enabling required

Usage (record a pre-execution approval):
    policy = regulated_strict_policy()
    record = policy.record_approval(
        dispatch_id="d-001",
        approved_by="operator",
        rationale="Review complete, approved for execution",
        approval_type=ApprovalType.PRE_EXECUTION,
    )
    assert record.approval_id.startswith("appr-")

Usage (check closeout requirements):
    state = DispatchApprovalState(dispatch_id="d-001")
    state.transition_to(ApprovalState.APPROVED)
    state.transition_to(ApprovalState.EXECUTING)
    state.transition_to(ApprovalState.PENDING_REVIEW)
    # Cannot close without a closure record:
    assert not policy.can_close(state)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def is_regulated_strict_enabled() -> bool:
    """Check if regulated_strict profile is enabled (pilot mode)."""
    return os.environ.get("VNX_REGULATED_STRICT_ENABLED", "0") == "1"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ApprovalState(Enum):
    """Dispatch lifecycle states for regulated_strict governance.

    Per contract Section 2.2 state machine.

    PENDING_APPROVAL — Dispatch registered, awaiting explicit operator approval.
    APPROVED         — Operator approved execution.
    EXECUTING        — Worker executing the dispatch.
    PENDING_REVIEW   — Execution complete, awaiting post-execution review.
    CLOSED           — Operator reviewed and accepted (terminal).
    REJECTED         — Operator rejected the dispatch (terminal).
    REVIEW_FAILED    — Post-execution review found issues; loops to PENDING_APPROVAL.
    """
    PENDING_APPROVAL = "pending_approval"
    APPROVED         = "approved"
    EXECUTING        = "executing"
    PENDING_REVIEW   = "pending_review"
    CLOSED           = "closed"
    REJECTED         = "rejected"
    REVIEW_FAILED    = "review_failed"


# Terminal states that cannot be transitioned out of (except REVIEW_FAILED).
TERMINAL_STATES = frozenset({ApprovalState.CLOSED, ApprovalState.REJECTED})

# Valid state transitions per contract state machine.
VALID_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.PENDING_APPROVAL: frozenset({
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
    }),
    ApprovalState.APPROVED: frozenset({
        ApprovalState.EXECUTING,
        ApprovalState.REJECTED,
    }),
    ApprovalState.EXECUTING: frozenset({
        ApprovalState.PENDING_REVIEW,
    }),
    ApprovalState.PENDING_REVIEW: frozenset({
        ApprovalState.CLOSED,
        ApprovalState.REVIEW_FAILED,
    }),
    ApprovalState.CLOSED: frozenset(),  # terminal
    ApprovalState.REJECTED: frozenset(),  # terminal
    ApprovalState.REVIEW_FAILED: frozenset({
        ApprovalState.PENDING_APPROVAL,  # loop back for re-approval
    }),
}


class ApprovalType(Enum):
    """Classification of approval records.

    PRE_EXECUTION — approval given before worker execution begins.
    POST_REVIEW   — closure approval given after reviewing execution results.
    """
    PRE_EXECUTION = "pre_execution"
    POST_REVIEW   = "post_review"


class ClosureType(Enum):
    """Classification of dispatch closure decisions.

    APPROVED  — operator reviewed and accepted execution results.
    REJECTED  — operator rejected; dispatch terminates without acceptance.
    EXCEPTION — operator closed with documented exception/residual risk.
    """
    APPROVED  = "approved"
    REJECTED  = "rejected"
    EXCEPTION = "exception"


# Valid approver identities (RA-2: automated approvals are forbidden).
VALID_APPROVERS = frozenset({"operator", "T0"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ApprovalError(Exception):
    """Base error for regulated_strict approval violations."""


class EmptyRationaleError(ApprovalError):
    """RA-1: rationale must be non-empty."""


class AutomatedApprovalError(ApprovalError):
    """RA-2: automated approvals are forbidden."""


class InvalidStateTransitionError(ApprovalError):
    """Raised when a state transition is not permitted by the state machine."""


class ClosureBlockedError(ApprovalError):
    """Raised when closure requirements are not met (RA-4)."""


# ---------------------------------------------------------------------------
# Approval record (immutable, RA-1..RA-3)
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable approval record per contract Section 2.3.

    Invariants enforced at construction:
      - RA-1: rationale is non-empty.
      - RA-2: approved_by is "operator" or "T0".
      - RA-3: record is frozen (dataclass frozen=True).

    Attributes:
        approval_id:   Unique identifier (format: "appr-<uuid4>").
        dispatch_id:   The dispatch this approval is for.
        approved_by:   Approver identity — must be "operator" or "T0".
        approved_at:   ISO 8601 timestamp of approval.
        approval_type: PRE_EXECUTION or POST_REVIEW.
        rationale:     Non-empty string explaining the approval decision.
        evidence_refs: Optional list of evidence references (gate IDs, etc.).
        conditions:    Optional list of conditions attached to the approval.
    """
    approval_id:   str
    dispatch_id:   str
    approved_by:   str
    approved_at:   str
    approval_type: ApprovalType
    rationale:     str
    evidence_refs: tuple = field(default_factory=tuple)
    conditions:    tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # RA-1: rationale must be non-empty
        if not self.rationale or not self.rationale.strip():
            raise EmptyRationaleError(
                "ApprovalRecord.rationale must be non-empty. "
                "Approvals without a rationale are rejected under regulated_strict policy (RA-1)."
            )
        # RA-2: approved_by must be operator or T0
        if self.approved_by not in VALID_APPROVERS:
            raise AutomatedApprovalError(
                f"ApprovalRecord.approved_by must be one of {sorted(VALID_APPROVERS)}. "
                f"Got: {self.approved_by!r}. "
                "Automated approvals are forbidden under regulated_strict policy (RA-2)."
            )

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict per contract Section 2.3 schema."""
        return {
            "approval_id":   self.approval_id,
            "dispatch_id":   self.dispatch_id,
            "approved_by":   self.approved_by,
            "approved_at":   self.approved_at,
            "approval_type": self.approval_type.value,
            "rationale":     self.rationale,
            "evidence_refs": list(self.evidence_refs),
            "conditions":    list(self.conditions),
        }


# ---------------------------------------------------------------------------
# Closure record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClosureRecord:
    """Explicit post-review closure record per contract Section 4.3.

    Required for dispatch to transition to CLOSED state.

    Attributes:
        closure_id:          Unique identifier (format: "close-<uuid4>").
        dispatch_id:         The dispatch being closed.
        closed_by:           Operator or T0 who recorded the closure.
        closed_at:           ISO 8601 timestamp.
        closure_type:        APPROVED, REJECTED, or EXCEPTION.
        rationale:           Non-empty string (required).
        bundle_id:           Reference to the audit bundle (may be empty for PR-1).
        bundle_complete:     Whether the audit bundle is complete.
        open_items_resolved: Whether all open items are resolved.
        residual_risks:      Documented residual risks (empty = none).
    """
    closure_id:          str
    dispatch_id:         str
    closed_by:           str
    closed_at:           str
    closure_type:        ClosureType
    rationale:           str
    bundle_id:           str = ""
    bundle_complete:     bool = False
    open_items_resolved: bool = True
    residual_risks:      tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.rationale or not self.rationale.strip():
            raise EmptyRationaleError(
                "ClosureRecord.rationale must be non-empty. "
                "Closures without a rationale are rejected under regulated_strict policy."
            )
        if self.closed_by not in VALID_APPROVERS:
            raise AutomatedApprovalError(
                f"ClosureRecord.closed_by must be one of {sorted(VALID_APPROVERS)}. "
                f"Got: {self.closed_by!r}. "
                "Automated closure is forbidden under regulated_strict policy."
            )

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict per contract Section 4.3 schema."""
        return {
            "closure_id":          self.closure_id,
            "dispatch_id":         self.dispatch_id,
            "closed_by":           self.closed_by,
            "closed_at":           self.closed_at,
            "closure_type":        self.closure_type.value,
            "rationale":           self.rationale,
            "bundle_id":           self.bundle_id,
            "bundle_complete":     self.bundle_complete,
            "open_items_resolved": self.open_items_resolved,
            "residual_risks":      list(self.residual_risks),
        }


# ---------------------------------------------------------------------------
# Dispatch approval state machine
# ---------------------------------------------------------------------------

@dataclass
class DispatchApprovalState:
    """Mutable approval state machine for a single regulated_strict dispatch.

    Tracks state, approval records, and closure record for one dispatch.
    Enforces valid state transitions per the contract state machine.

    Attributes:
        dispatch_id:       The dispatch being tracked.
        state:             Current ApprovalState (starts at PENDING_APPROVAL).
        pre_approvals:     List of pre-execution ApprovalRecord instances.
        closure_record:    The post-review closure record (None until recorded).
    """
    dispatch_id:    str
    state:          ApprovalState = field(default=ApprovalState.PENDING_APPROVAL)
    pre_approvals:  List[ApprovalRecord] = field(default_factory=list)
    closure_record: Optional[ClosureRecord] = None

    def transition_to(self, new_state: ApprovalState) -> None:
        """Transition to a new state.

        Raises:
            InvalidStateTransitionError: If the transition is not valid.
        """
        allowed = VALID_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot transition from {self.state.value!r} to {new_state.value!r}. "
                f"Allowed transitions from {self.state.value!r}: "
                f"{sorted(s.value for s in allowed) or 'none (terminal state)'}."
            )
        self.state = new_state

    def add_pre_approval(self, record: ApprovalRecord) -> None:
        """Record a pre-execution approval for this dispatch."""
        if record.approval_type != ApprovalType.PRE_EXECUTION:
            raise ApprovalError(
                f"Expected PRE_EXECUTION approval, got {record.approval_type.value!r}. "
                "Use apply_closure() for post-review approvals."
            )
        self.pre_approvals.append(record)

    def apply_closure(self, record: ClosureRecord) -> None:
        """Record a post-review closure.

        Raises:
            ApprovalError: If dispatch is not in PENDING_REVIEW state.
        """
        if self.state != ApprovalState.PENDING_REVIEW:
            raise ApprovalError(
                f"Cannot apply closure in state {self.state.value!r}. "
                "Dispatch must be in PENDING_REVIEW state to accept a closure record."
            )
        self.closure_record = record

    def has_pre_execution_approval(self) -> bool:
        """True if at least one pre-execution approval is recorded (RA-4)."""
        return len(self.pre_approvals) > 0

    def has_closure_record(self) -> bool:
        """True if a post-review closure record is recorded (RA-4)."""
        return self.closure_record is not None

    def to_summary(self) -> dict:
        """Operator-readable summary of current approval state."""
        return {
            "dispatch_id":              self.dispatch_id,
            "state":                    self.state.value,
            "pre_approval_count":       len(self.pre_approvals),
            "has_pre_approval":         self.has_pre_execution_approval(),
            "has_closure_record":       self.has_closure_record(),
            "closure_type":             (self.closure_record.closure_type.value
                                         if self.closure_record else None),
        }


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegulatedStrictApprovalPolicy:
    """Enforcement engine for regulated_strict approval workflow.

    Enforces:
      - RA-1: rationale required on every approval
      - RA-2: automated approvals forbidden
      - RA-3: records are immutable (enforced by frozen dataclasses)
      - RA-4: both pre-execution approval and post-review closure required for closure

    Does NOT affect coding_strict or business_light governance paths.
    """

    def record_approval(
        self,
        *,
        dispatch_id: str,
        approved_by: str,
        rationale: str,
        approval_type: ApprovalType = ApprovalType.PRE_EXECUTION,
        evidence_refs: Optional[List[str]] = None,
        conditions: Optional[List[str]] = None,
    ) -> ApprovalRecord:
        """Create and return an immutable ApprovalRecord.

        Enforces RA-1 (non-empty rationale) and RA-2 (valid approver identity).
        The caller is responsible for storing the record.

        Args:
            dispatch_id:   The dispatch being approved.
            approved_by:   Approver — must be "operator" or "T0".
            rationale:     Non-empty explanation of the approval decision.
            approval_type: PRE_EXECUTION (default) or POST_REVIEW.
            evidence_refs: Optional evidence reference IDs.
            conditions:    Optional conditions attached to this approval.

        Returns:
            Immutable ApprovalRecord with generated approval_id and timestamp.

        Raises:
            EmptyRationaleError:    If rationale is empty (RA-1).
            AutomatedApprovalError: If approved_by is not operator or T0 (RA-2).
        """
        return ApprovalRecord(
            approval_id=f"appr-{uuid.uuid4()}",
            dispatch_id=dispatch_id,
            approved_by=approved_by,
            approved_at=_now_utc_iso(),
            approval_type=approval_type,
            rationale=rationale,
            evidence_refs=tuple(evidence_refs) if evidence_refs else (),
            conditions=tuple(conditions) if conditions else (),
        )

    def record_closure(
        self,
        *,
        dispatch_id: str,
        closed_by: str,
        rationale: str,
        closure_type: ClosureType = ClosureType.APPROVED,
        bundle_id: str = "",
        bundle_complete: bool = False,
        open_items_resolved: bool = True,
        residual_risks: Optional[List[str]] = None,
    ) -> ClosureRecord:
        """Create and return an immutable ClosureRecord.

        Args:
            dispatch_id:         The dispatch being closed.
            closed_by:           Must be "operator" or "T0".
            rationale:           Non-empty explanation.
            closure_type:        APPROVED, REJECTED, or EXCEPTION.
            bundle_id:           Audit bundle reference.
            bundle_complete:     Whether the bundle is complete.
            open_items_resolved: Whether all open items are resolved.
            residual_risks:      List of documented residual risks.

        Returns:
            Immutable ClosureRecord with generated closure_id and timestamp.

        Raises:
            EmptyRationaleError:    If rationale is empty.
            AutomatedApprovalError: If closed_by is not operator or T0.
        """
        return ClosureRecord(
            closure_id=f"close-{uuid.uuid4()}",
            dispatch_id=dispatch_id,
            closed_by=closed_by,
            closed_at=_now_utc_iso(),
            closure_type=closure_type,
            rationale=rationale,
            bundle_id=bundle_id,
            bundle_complete=bundle_complete,
            open_items_resolved=open_items_resolved,
            residual_risks=tuple(residual_risks) if residual_risks else (),
        )

    def can_close(self, state: DispatchApprovalState) -> bool:
        """Check whether a dispatch meets all closure requirements.

        Per contract Section 4.2, closure requires:
          1. At least one pre-execution approval (RA-4)
          2. Dispatch is in PENDING_REVIEW state
          3. A closure record has been recorded (RA-4)

        This check covers requirements 1, 2, 3. Requirements 4 (audit bundle)
        and 5 (gate results) are enforced by later PRs.

        Returns True only if all three conditions are met.
        """
        return (
            state.has_pre_execution_approval()
            and state.state == ApprovalState.PENDING_REVIEW
            and state.has_closure_record()
        )

    def assert_can_close(self, state: DispatchApprovalState) -> None:
        """Assert closure requirements are met, raising ClosureBlockedError if not.

        Raises:
            ClosureBlockedError: With a description of what requirement is missing.
        """
        missing: List[str] = []

        if not state.has_pre_execution_approval():
            missing.append(
                "no pre-execution approval record (RA-4: at least one required)"
            )
        if state.state != ApprovalState.PENDING_REVIEW:
            missing.append(
                f"dispatch is in state {state.state.value!r}, expected 'pending_review'"
            )
        if not state.has_closure_record():
            missing.append(
                "no post-review closure record (RA-4: explicit closure required)"
            )

        if missing:
            raise ClosureBlockedError(
                f"Dispatch {state.dispatch_id!r} cannot close. "
                f"Missing requirements: {'; '.join(missing)}."
            )

    def transition_dispatch(
        self,
        state: DispatchApprovalState,
        new_state: ApprovalState,
    ) -> None:
        """Transition a dispatch state, enforcing state machine rules.

        Convenience wrapper around DispatchApprovalState.transition_to().

        Raises:
            InvalidStateTransitionError: If transition is not permitted.
        """
        state.transition_to(new_state)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def regulated_strict_policy() -> RegulatedStrictApprovalPolicy:
    """Return the canonical regulated_strict approval policy instance."""
    return RegulatedStrictApprovalPolicy()
