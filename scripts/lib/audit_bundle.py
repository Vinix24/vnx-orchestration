#!/usr/bin/env python3
"""Audit bundle builder for regulated_strict governance runs (Feature 17, PR-2).

Packages all evidence from a single dispatch run into an immutable, auditable
bundle so operators can inspect or reconstruct execution history without manual
reconstruction.

Components:
  EvidenceType         — classification of supported evidence kinds
  EvidenceEntry        — immutable evidence item with id, type, timestamp, payload
  AuditBundle          — immutable bundle of all evidence for one dispatch run
  AuditBundleBuilder   — mutable builder that accumulates evidence before sealing
  AuditBundleError     — base error for bundle violations
  EmptyBundleError     — raised when building a bundle with no evidence
  audit_bundle_builder()  — factory returning a fresh builder for a dispatch

Design invariants:
  - AB-1: AuditBundle and EvidenceEntry are immutable (frozen dataclasses).
  - AB-2: Building a bundle is non-destructive — source objects are never mutated.
  - AB-3: Building a bundle with no evidence is rejected (EmptyBundleError).
  - AB-4: bundle_id format is "bundle-<uuid4>".
  - AB-5: entry_id format is "entry-<uuid4>".
  - AB-6: to_dict() returns only JSON-serializable primitive types.

Evidence types supported:
  - APPROVAL_RECORD  — from regulated_strict_approval.ApprovalRecord
  - CLOSURE_RECORD   — from regulated_strict_approval.ClosureRecord
  - GATE_RESULT      — dict with gate_id, outcome, timestamp
  - RECEIPT          — dict with receipt_id, dispatch_id, timestamp
  - RUNTIME_EVENT    — dict with event_type, timestamp, plus arbitrary payload

Completeness definition:
  A bundle is considered complete when it has at least one APPROVAL_RECORD,
  at least one CLOSURE_RECORD, and at least one GATE_RESULT or RECEIPT.

Usage:
    builder = audit_bundle_builder(dispatch_id="d-001")
    builder.add_approval(approval_record)
    builder.add_closure(closure_record)
    builder.add_gate_result({"gate_id": "g-001", "outcome": "pass", "timestamp": "..."})
    bundle = builder.build()
    assert bundle.bundle_id.startswith("bundle-")
    data = bundle.to_dict()  # fully JSON-serializable
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# Import source types — used only for type narrowing; no mutation performed.
from regulated_strict_approval import ApprovalRecord, ClosureRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_entry_id() -> str:
    """Generate a unique entry ID in the format 'entry-<uuid4>'."""
    return f"entry-{uuid.uuid4()}"


def _new_bundle_id() -> str:
    """Generate a unique bundle ID in the format 'bundle-<uuid4>'."""
    return f"bundle-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AuditBundleError(Exception):
    """Base error for audit bundle violations."""


class EmptyBundleError(AuditBundleError):
    """AB-3: raised when attempting to build a bundle with no evidence."""


class InvalidEvidenceError(AuditBundleError):
    """Raised when an evidence item is missing required fields."""


# ---------------------------------------------------------------------------
# Evidence type classification
# ---------------------------------------------------------------------------

class EvidenceType(Enum):
    """Classification of supported evidence kinds.

    APPROVAL_RECORD — pre-execution or post-review approval from regulated_strict.
    CLOSURE_RECORD  — post-review closure decision from regulated_strict.
    GATE_RESULT     — quality gate outcome (gate_id, outcome, timestamp).
    RECEIPT         — dispatch execution receipt (receipt_id, dispatch_id, timestamp).
    RUNTIME_EVENT   — structured runtime lifecycle event (event_type, timestamp).
    """
    APPROVAL_RECORD = "approval_record"
    CLOSURE_RECORD  = "closure_record"
    GATE_RESULT     = "gate_result"
    RECEIPT         = "receipt"
    RUNTIME_EVENT   = "runtime_event"


# ---------------------------------------------------------------------------
# EvidenceEntry — immutable evidence item (AB-1, AB-5)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceEntry:
    """Immutable evidence item in an audit bundle.

    Each entry captures one piece of evidence with a stable unique ID,
    a type classification, an ISO 8601 timestamp, and a copy of the
    evidence payload.

    Invariants:
      - AB-1: frozen=True — immutable after creation.
      - AB-5: entry_id format is "entry-<uuid4>".
      - payload contains only JSON-serializable primitive types.

    Attributes:
        entry_id:      Unique identifier ("entry-<uuid4>").
        evidence_type: EvidenceType classification.
        timestamp:     ISO 8601 timestamp of when evidence was added to bundle.
        payload:       Dict of evidence data (JSON-serializable primitives only).
    """
    entry_id:      str
    evidence_type: EvidenceType
    timestamp:     str
    payload:       Dict[str, Any]

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict representation of this entry."""
        return {
            "entry_id":      self.entry_id,
            "evidence_type": self.evidence_type.value,
            "timestamp":     self.timestamp,
            "payload":       dict(self.payload),
        }


# ---------------------------------------------------------------------------
# AuditBundle — immutable bundle (AB-1, AB-4, AB-6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditBundle:
    """Immutable audit bundle for a single regulated_strict dispatch run.

    Packages all evidence collected for one dispatch into a single inspectable
    artifact. Once built, the bundle cannot be modified (AB-1).

    Invariants:
      - AB-1: frozen=True — immutable after creation.
      - AB-4: bundle_id format is "bundle-<uuid4>".
      - AB-6: to_dict() returns only JSON-serializable primitive types.

    Attributes:
        bundle_id:    Unique identifier ("bundle-<uuid4>").
        dispatch_id:  The dispatch this bundle covers.
        created_at:   ISO 8601 timestamp when the bundle was built.
        evidence:     Tuple of EvidenceEntry instances (the evidence index).
    """
    bundle_id:   str
    dispatch_id: str
    created_at:  str
    evidence:    Tuple[EvidenceEntry, ...]

    def is_complete(self) -> bool:
        """Check whether the bundle meets completeness requirements.

        A bundle is complete when it has:
          - at least one APPROVAL_RECORD
          - at least one CLOSURE_RECORD
          - at least one GATE_RESULT or RECEIPT

        Returns True only when all three conditions are met.
        """
        types = {e.evidence_type for e in self.evidence}
        has_approval  = EvidenceType.APPROVAL_RECORD in types
        has_closure   = EvidenceType.CLOSURE_RECORD in types
        has_gate_or_receipt = (
            EvidenceType.GATE_RESULT in types
            or EvidenceType.RECEIPT in types
        )
        return has_approval and has_closure and has_gate_or_receipt

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict representation of the bundle.

        All values are primitive types (str, bool, int, float, list, dict).
        Enum values are converted to their string representations.
        """
        return {
            "bundle_id":   self.bundle_id,
            "dispatch_id": self.dispatch_id,
            "created_at":  self.created_at,
            "is_complete": self.is_complete(),
            "evidence_count": len(self.evidence),
            "evidence": [e.to_dict() for e in self.evidence],
        }


# ---------------------------------------------------------------------------
# AuditBundleBuilder — mutable accumulator
# ---------------------------------------------------------------------------

@dataclass
class AuditBundleBuilder:
    """Mutable builder that accumulates evidence before sealing an AuditBundle.

    Use the add_* methods to attach evidence items from a dispatch run.
    Call build() to produce an immutable AuditBundle.

    All add_* methods are non-destructive: source objects (ApprovalRecord,
    ClosureRecord, dicts) are read but never mutated (AB-2).

    Attributes:
        dispatch_id: The dispatch this bundle covers.
    """
    dispatch_id: str
    _entries: List[EvidenceEntry] = field(default_factory=list, init=False, repr=False)

    # -------------------------------------------------------------------
    # Evidence addition methods
    # -------------------------------------------------------------------

    def add_approval(self, record: ApprovalRecord) -> "AuditBundleBuilder":
        """Add an ApprovalRecord as evidence.

        Reads record via to_dict() — does not mutate the source (AB-2).

        Args:
            record: An immutable ApprovalRecord from regulated_strict_approval.

        Returns:
            self, for chaining.

        Raises:
            InvalidEvidenceError: If record is not an ApprovalRecord.
        """
        if not isinstance(record, ApprovalRecord):
            raise InvalidEvidenceError(
                f"add_approval() expects an ApprovalRecord, got {type(record).__name__!r}."
            )
        if record.dispatch_id != self.dispatch_id:
            raise ValueError(
                f"Approval dispatch_id {record.dispatch_id!r} does not match "
                f"builder dispatch_id {self.dispatch_id!r}. "
                "Cross-dispatch evidence is not permitted."
            )
        entry = EvidenceEntry(
            entry_id=_new_entry_id(),
            evidence_type=EvidenceType.APPROVAL_RECORD,
            timestamp=_now_utc_iso(),
            payload=record.to_dict(),
        )
        self._entries.append(entry)
        return self

    def add_closure(self, record: ClosureRecord) -> "AuditBundleBuilder":
        """Add a ClosureRecord as evidence.

        Reads record via to_dict() — does not mutate the source (AB-2).

        Args:
            record: An immutable ClosureRecord from regulated_strict_approval.

        Returns:
            self, for chaining.

        Raises:
            InvalidEvidenceError: If record is not a ClosureRecord.
        """
        if not isinstance(record, ClosureRecord):
            raise InvalidEvidenceError(
                f"add_closure() expects a ClosureRecord, got {type(record).__name__!r}."
            )
        if record.dispatch_id != self.dispatch_id:
            raise ValueError(
                f"Closure dispatch_id {record.dispatch_id!r} does not match "
                f"builder dispatch_id {self.dispatch_id!r}. "
                "Cross-dispatch evidence is not permitted."
            )
        entry = EvidenceEntry(
            entry_id=_new_entry_id(),
            evidence_type=EvidenceType.CLOSURE_RECORD,
            timestamp=_now_utc_iso(),
            payload=record.to_dict(),
        )
        self._entries.append(entry)
        return self

    def add_gate_result(self, gate_result: Dict[str, Any]) -> "AuditBundleBuilder":
        """Add a gate result dict as evidence.

        Required fields: gate_id, outcome, timestamp.

        Args:
            gate_result: Dict containing at minimum gate_id, outcome, timestamp.

        Returns:
            self, for chaining.

        Raises:
            InvalidEvidenceError: If required fields are missing.
        """
        _require_fields(gate_result, ("gate_id", "outcome", "timestamp", "dispatch_id"), "gate_result")
        if gate_result["dispatch_id"] != self.dispatch_id:
            raise ValueError(
                f"Gate result dispatch_id {gate_result['dispatch_id']!r} does not match "
                f"builder dispatch_id {self.dispatch_id!r}. "
                "Cross-dispatch evidence is not permitted."
            )
        entry = EvidenceEntry(
            entry_id=_new_entry_id(),
            evidence_type=EvidenceType.GATE_RESULT,
            timestamp=_now_utc_iso(),
            payload=dict(gate_result),
        )
        self._entries.append(entry)
        return self

    def add_receipt(self, receipt: Dict[str, Any]) -> "AuditBundleBuilder":
        """Add a receipt dict as evidence.

        Required fields: receipt_id, dispatch_id, timestamp.

        Args:
            receipt: Dict containing at minimum receipt_id, dispatch_id, timestamp.

        Returns:
            self, for chaining.

        Raises:
            InvalidEvidenceError: If required fields are missing.
        """
        _require_fields(receipt, ("receipt_id", "dispatch_id", "timestamp"), "receipt")
        if receipt["dispatch_id"] != self.dispatch_id:
            raise ValueError(
                f"Receipt dispatch_id {receipt['dispatch_id']!r} does not match "
                f"builder dispatch_id {self.dispatch_id!r}. "
                "Cross-dispatch evidence is not permitted."
            )
        entry = EvidenceEntry(
            entry_id=_new_entry_id(),
            evidence_type=EvidenceType.RECEIPT,
            timestamp=_now_utc_iso(),
            payload=dict(receipt),
        )
        self._entries.append(entry)
        return self

    def add_runtime_event(self, event: Dict[str, Any]) -> "AuditBundleBuilder":
        """Add a runtime event dict as evidence.

        Required fields: event_type, timestamp. Arbitrary additional keys
        are preserved in the payload.

        Args:
            event: Dict containing at minimum event_type and timestamp,
                   plus any additional runtime-specific fields.

        Returns:
            self, for chaining.

        Raises:
            InvalidEvidenceError: If required fields are missing.
        """
        _require_fields(event, ("event_type", "timestamp", "dispatch_id"), "runtime_event")
        if event["dispatch_id"] != self.dispatch_id:
            raise ValueError(
                f"Runtime event dispatch_id {event['dispatch_id']!r} does not match "
                f"builder dispatch_id {self.dispatch_id!r}. "
                "Cross-dispatch evidence is not permitted."
            )
        entry = EvidenceEntry(
            entry_id=_new_entry_id(),
            evidence_type=EvidenceType.RUNTIME_EVENT,
            timestamp=_now_utc_iso(),
            payload=dict(event),
        )
        self._entries.append(entry)
        return self

    # -------------------------------------------------------------------
    # Build
    # -------------------------------------------------------------------

    def build(self) -> AuditBundle:
        """Seal all accumulated evidence into an immutable AuditBundle.

        The builder's internal list is copied into an immutable tuple before
        being placed into the frozen AuditBundle. The builder itself is
        unchanged after calling build() and may not be reused.

        Returns:
            AuditBundle with a unique bundle_id and all evidence indexed.

        Raises:
            EmptyBundleError: If no evidence has been added (AB-3).
        """
        if not self._entries:
            raise EmptyBundleError(
                f"Cannot build an AuditBundle for dispatch {self.dispatch_id!r} "
                "with no evidence. Add at least one evidence item before calling build()."
            )
        return AuditBundle(
            bundle_id=_new_bundle_id(),
            dispatch_id=self.dispatch_id,
            created_at=_now_utc_iso(),
            evidence=tuple(self._entries),
        )


# ---------------------------------------------------------------------------
# Internal validation helper
# ---------------------------------------------------------------------------

def _require_fields(
    data: Dict[str, Any],
    required: Tuple[str, ...],
    kind: str,
) -> None:
    """Raise InvalidEvidenceError if any required field is missing from data."""
    missing = [f for f in required if f not in data]
    if missing:
        raise InvalidEvidenceError(
            f"Evidence of kind {kind!r} is missing required fields: "
            f"{missing}. Provided keys: {sorted(data.keys())}."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def audit_bundle_builder(dispatch_id: str) -> AuditBundleBuilder:
    """Return a fresh AuditBundleBuilder for the given dispatch.

    Args:
        dispatch_id: The dispatch ID this bundle will cover.

    Returns:
        A new AuditBundleBuilder with no evidence accumulated yet.

    Raises:
        ValueError: If dispatch_id is empty.
    """
    if not dispatch_id or not dispatch_id.strip():
        raise ValueError(
            "dispatch_id must be a non-empty string. "
            "An audit bundle must be tied to a specific dispatch."
        )
    return AuditBundleBuilder(dispatch_id=dispatch_id)
