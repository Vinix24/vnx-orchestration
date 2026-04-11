#!/usr/bin/env python3
"""Business-light review and closeout policy (Feature 20, PR-2).

Implements a softer review-by-exception policy for business_light governance:
tasks can progress without a full review unless blocking open items are present,
while audit artifacts are always retained and closeouts always require an
explicit manager decision.

Components:
  ReviewMode            — FULL_REVIEW | REVIEW_BY_EXCEPTION
  OpenItemSeverity      — BLOCKER | WARNING | INFO
  AuditArtifactType     — classification of retained evidence
  OpenItem              — individual open issue with severity and resolution state
  AuditArtifact         — immutable piece of retained evidence
  AuditRecord           — immutable ordered collection of artifacts for a task
  GateResult            — outcome of a review gate check
  CloseoutDecision      — explicit manager decision required to close a task
  BusinessLightReviewPolicy — the core policy engine
  business_light_policy()  — factory returning the canonical policy instance

Design invariants:
  - BLOCKER open items always block can_proceed() and gate_result().
  - WARNING and INFO items are retained in audit but never block progress.
  - CloseoutDecision.is_explicit is always True; False raises ValueError.
  - AuditRecord is immutable: with_artifact() returns a new record.
  - Audit artifacts are always retained — even when can_proceed() is True.
  - No silent auto-closeouts are possible by construction.

Usage (check whether a task can proceed):
    policy = business_light_policy()
    items = [OpenItem("i1", "missing doc", OpenItemSeverity.WARNING)]
    assert policy.can_proceed(items)   # WARNING does not block

    items.append(OpenItem("i2", "security gap", OpenItemSeverity.BLOCKER))
    assert not policy.can_proceed(items)   # BLOCKER blocks

Usage (close a task with audit trail):
    record = AuditRecord(task_id="d-001")
    record = record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING,
                                                 note="all checks passed"))
    decision = CloseoutDecision(task_id="d-001", decided_by="manager-T0")
    record = policy.apply_closeout(decision, record)
    assert len(record.find_by_type(AuditArtifactType.CLOSEOUT_DECISION)) == 1
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List

# ---------------------------------------------------------------------------
# Delegation to governance_profiles (thin wrapper)
# ---------------------------------------------------------------------------
# governance_profiles is the canonical source for profile config.
# business_light_policy re-exports the load/resolve API for backward compat.
try:
    _lib_dir = os.path.dirname(os.path.abspath(__file__))
    if _lib_dir not in sys.path:
        sys.path.insert(0, _lib_dir)
    from governance_profiles import (  # noqa: F401  (re-export)
        GovernanceProfile,
        load_profiles,
        resolve_profile,
    )
    _GOVERNANCE_PROFILES_AVAILABLE = True
except ImportError:
    _GOVERNANCE_PROFILES_AVAILABLE = False


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ReviewMode(Enum):
    """How the review gate operates.

    FULL_REVIEW        — every task requires an explicit review before proceeding.
    REVIEW_BY_EXCEPTION — tasks proceed unless blocking items require review.
    """
    FULL_REVIEW        = "full_review"
    REVIEW_BY_EXCEPTION = "review_by_exception"


class OpenItemSeverity(Enum):
    """Severity classification of an open item.

    BLOCKER — must be resolved before the task can proceed.
    WARNING — recorded in audit; does not block progress.
    INFO    — informational; retained for continuity; does not block.
    """
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO    = "info"


class AuditArtifactType(Enum):
    """Classification of a retained audit artifact."""
    REVIEW_DECISION   = "review_decision"
    OPEN_ITEM         = "open_item"
    CLOSEOUT_DECISION = "closeout_decision"
    FINDING           = "finding"
    RETENTION_RECEIPT = "retention_receipt"


# ---------------------------------------------------------------------------
# Open item
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenItem:
    """An individual open issue with severity and resolution tracking.

    Attributes:
        item_id:             Unique identifier.
        description:         Human-readable description of the issue.
        severity:            BLOCKER, WARNING, or INFO.
        requires_resolution: If True, item must be resolved to unblock (default True).
        is_resolved:         Whether the item has been explicitly resolved.
    """
    item_id: str
    description: str
    severity: OpenItemSeverity
    requires_resolution: bool = True
    is_resolved: bool = False

    def is_blocking(self) -> bool:
        """True if this item blocks task progress."""
        return (self.severity == OpenItemSeverity.BLOCKER
                and self.requires_resolution
                and not self.is_resolved)


# ---------------------------------------------------------------------------
# Audit artifact and record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditArtifact:
    """Immutable piece of retained evidence.

    Attributes:
        artifact_id:   Unique identifier.
        artifact_type: Classification (e.g., FINDING, CLOSEOUT_DECISION).
        note:          Human-readable note attached to this artifact.
    """
    artifact_id: str
    artifact_type: AuditArtifactType
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type.value,
            "note": self.note,
        }


@dataclass(frozen=True)
class AuditRecord:
    """Immutable ordered collection of audit artifacts for a task.

    Attributes:
        task_id:   The task this record belongs to.
        artifacts: Tuple of retained AuditArtifact instances.
    """
    task_id: str
    artifacts: tuple = field(default_factory=tuple)

    def find_by_type(self, artifact_type: AuditArtifactType) -> List[AuditArtifact]:
        """Return all artifacts of the given type."""
        return [a for a in self.artifacts if a.artifact_type == artifact_type]

    def is_empty(self) -> bool:
        """True if no artifacts have been retained."""
        return len(self.artifacts) == 0

    def with_artifact(self, artifact: AuditArtifact) -> AuditRecord:
        """Return a new AuditRecord with the artifact appended."""
        return AuditRecord(task_id=self.task_id,
                           artifacts=self.artifacts + (artifact,))


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Result of a review gate check.

    Attributes:
        passed:         True if there are no blocking items.
        blocking_items: Open items that caused a block (empty when passed=True).
        record:         The audit record at the time of the gate check.
    """
    passed: bool
    blocking_items: tuple
    record: AuditRecord

    def is_blocked(self) -> bool:
        """True when the gate did not pass."""
        return not self.passed


# ---------------------------------------------------------------------------
# Closeout decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloseoutDecision:
    """Explicit manager decision to close out a task.

    INVARIANT: is_explicit is always True.
    A silent auto-closeout is not possible under business_light policy.
    """
    task_id: str
    decided_by: str
    is_explicit: bool = True

    def __post_init__(self) -> None:
        if not self.is_explicit:
            raise ValueError(
                "CloseoutDecision.is_explicit must be True. "
                "Automatic closeouts are not permitted under business_light policy. "
                "An explicit manager decision is always required."
            )


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessLightReviewPolicy:
    """Review-by-exception policy for the business_light governance profile.

    Tasks can proceed as long as there are no unresolved BLOCKER items.
    WARNING and INFO items are retained in audit but never block progress.
    Closeouts always require an explicit CloseoutDecision.
    """
    review_mode: ReviewMode = ReviewMode.REVIEW_BY_EXCEPTION

    def can_proceed(self, open_items: List[OpenItem]) -> bool:
        """True if no open item is currently blocking."""
        return not any(item.is_blocking() for item in open_items)

    def gate_result(
        self,
        open_items: List[OpenItem],
        record: AuditRecord,
    ) -> GateResult:
        """Run the review gate and return a GateResult with the current record."""
        blocking = tuple(item for item in open_items if item.is_blocking())
        return GateResult(passed=not blocking, blocking_items=blocking, record=record)

    def apply_closeout(
        self,
        decision: CloseoutDecision,
        record: AuditRecord,
    ) -> AuditRecord:
        """Apply a closeout decision and return an updated AuditRecord.

        The decision's is_explicit invariant is enforced in CloseoutDecision.__post_init__.
        This method appends a CLOSEOUT_DECISION artifact to the record.
        """
        artifact = AuditArtifact(
            artifact_id=f"closeout-{decision.task_id}",
            artifact_type=AuditArtifactType.CLOSEOUT_DECISION,
            note=f"Explicit closeout by {decision.decided_by}",
        )
        return record.with_artifact(artifact)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def business_light_policy() -> BusinessLightReviewPolicy:
    """Return the canonical business_light review policy instance."""
    return BusinessLightReviewPolicy(review_mode=ReviewMode.REVIEW_BY_EXCEPTION)
