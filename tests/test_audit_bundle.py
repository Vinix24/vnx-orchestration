#!/usr/bin/env python3
"""Tests for audit bundle builder and evidence index (Feature 17, PR-2).

Covers all success criteria from the dispatch:
  1.  Bundle has a unique bundle_id ("bundle-<uuid4>")
  2.  Evidence index contains all added entries
  3.  Bundle is non-destructive — source objects unchanged after building
  4.  Empty bundle rejected (EmptyBundleError)
  5.  to_dict() is fully JSON-serializable
  6.  Bundle completeness check works (approval + closure + gate/receipt)
  7.  Each EvidenceEntry has: entry_id, evidence_type, timestamp, payload
  8.  EvidenceType enum — all five kinds covered
  9.  AuditBundleBuilder — all add_* methods, chaining, build
  10. AuditBundle — immutability (frozen=True)
  11. EvidenceEntry — immutability (frozen=True)
  12. Field validation — missing required fields raise InvalidEvidenceError
  13. Factory — audit_bundle_builder() validates dispatch_id
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from audit_bundle import (
    AuditBundle,
    AuditBundleBuilder,
    AuditBundleError,
    EmptyBundleError,
    EvidenceEntry,
    EvidenceType,
    InvalidEvidenceError,
    audit_bundle_builder,
)
from regulated_strict_approval import (
    ApprovalRecord,
    ApprovalType,
    ClosureRecord,
    ClosureType,
    regulated_strict_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy():
    return regulated_strict_policy()


def _make_approval(dispatch_id: str = "d-001") -> ApprovalRecord:
    return _policy().record_approval(
        dispatch_id=dispatch_id,
        approved_by="operator",
        rationale="Reviewed and approved for execution",
        approval_type=ApprovalType.PRE_EXECUTION,
    )


def _make_closure(dispatch_id: str = "d-001") -> ClosureRecord:
    return _policy().record_closure(
        dispatch_id=dispatch_id,
        closed_by="operator",
        rationale="Execution reviewed and accepted",
    )


def _make_gate_result(gate_id: str = "g-001", dispatch_id: str = "d-001") -> dict:
    return {
        "gate_id": gate_id,
        "outcome": "pass",
        "timestamp": "2026-04-03T15:00:00+00:00",
        "dispatch_id": dispatch_id,
    }


def _make_receipt(receipt_id: str = "r-001", dispatch_id: str = "d-001") -> dict:
    return {
        "receipt_id": receipt_id,
        "dispatch_id": dispatch_id,
        "timestamp": "2026-04-03T15:01:00+00:00",
    }


def _make_runtime_event(event_type: str = "session_start", dispatch_id: str = "d-001") -> dict:
    return {
        "event_type": event_type,
        "timestamp": "2026-04-03T15:02:00+00:00",
        "session_id": "sess-abc",
        "dispatch_id": dispatch_id,
    }


def _complete_builder(dispatch_id: str = "d-001") -> AuditBundleBuilder:
    """Return a builder with one of every evidence type added."""
    builder = audit_bundle_builder(dispatch_id)
    builder.add_approval(_make_approval(dispatch_id))
    builder.add_closure(_make_closure(dispatch_id))
    builder.add_gate_result(_make_gate_result(dispatch_id=dispatch_id))
    builder.add_receipt(_make_receipt(dispatch_id=dispatch_id))
    builder.add_runtime_event(_make_runtime_event(dispatch_id=dispatch_id))
    return builder


# ---------------------------------------------------------------------------
# 1. bundle_id uniqueness and format
# ---------------------------------------------------------------------------

class TestBundleId:

    def test_bundle_id_starts_with_bundle_prefix(self) -> None:
        bundle = _complete_builder().build()
        assert bundle.bundle_id.startswith("bundle-")

    def test_bundle_id_has_uuid_portion(self) -> None:
        bundle = _complete_builder().build()
        # "bundle-" is 7 chars; uuid4 is 36 chars
        assert len(bundle.bundle_id) == len("bundle-") + 36

    def test_bundle_ids_are_unique(self) -> None:
        b1 = _complete_builder().build()
        b2 = _complete_builder().build()
        assert b1.bundle_id != b2.bundle_id

    def test_bundle_carries_dispatch_id(self) -> None:
        bundle = _complete_builder("dispatch-xyz").build()
        assert bundle.dispatch_id == "dispatch-xyz"

    def test_bundle_has_created_at(self) -> None:
        bundle = _complete_builder().build()
        assert "T" in bundle.created_at or "-" in bundle.created_at


# ---------------------------------------------------------------------------
# 2. Evidence index contains all added entries
# ---------------------------------------------------------------------------

class TestEvidenceIndex:

    def test_evidence_count_matches_added(self) -> None:
        bundle = _complete_builder().build()
        # complete_builder adds 5 items (approval, closure, gate, receipt, event)
        assert len(bundle.evidence) == 5

    def test_evidence_contains_approval_entry(self) -> None:
        bundle = _complete_builder().build()
        types = {e.evidence_type for e in bundle.evidence}
        assert EvidenceType.APPROVAL_RECORD in types

    def test_evidence_contains_closure_entry(self) -> None:
        bundle = _complete_builder().build()
        types = {e.evidence_type for e in bundle.evidence}
        assert EvidenceType.CLOSURE_RECORD in types

    def test_evidence_contains_gate_result_entry(self) -> None:
        bundle = _complete_builder().build()
        types = {e.evidence_type for e in bundle.evidence}
        assert EvidenceType.GATE_RESULT in types

    def test_evidence_contains_receipt_entry(self) -> None:
        bundle = _complete_builder().build()
        types = {e.evidence_type for e in bundle.evidence}
        assert EvidenceType.RECEIPT in types

    def test_evidence_contains_runtime_event_entry(self) -> None:
        bundle = _complete_builder().build()
        types = {e.evidence_type for e in bundle.evidence}
        assert EvidenceType.RUNTIME_EVENT in types

    def test_multiple_approvals_all_indexed(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        bundle = builder.build()
        approval_entries = [e for e in bundle.evidence if e.evidence_type == EvidenceType.APPROVAL_RECORD]
        assert len(approval_entries) == 2

    def test_evidence_is_tuple(self) -> None:
        bundle = _complete_builder().build()
        assert isinstance(bundle.evidence, tuple)


# ---------------------------------------------------------------------------
# 3. Non-destructive — source objects unchanged after building
# ---------------------------------------------------------------------------

class TestNonDestructive:

    def test_approval_record_unchanged_after_build(self) -> None:
        approval = _make_approval()
        original_id = approval.approval_id
        original_rationale = approval.rationale
        builder = audit_bundle_builder("d-001")
        builder.add_approval(approval)
        builder.add_closure(_make_closure())
        builder.build()
        # Approval record values must be identical after bundle is built
        assert approval.approval_id == original_id
        assert approval.rationale == original_rationale

    def test_closure_record_unchanged_after_build(self) -> None:
        closure = _make_closure()
        original_id = closure.closure_id
        original_closure_type = closure.closure_type
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(closure)
        builder.build()
        assert closure.closure_id == original_id
        assert closure.closure_type == original_closure_type

    def test_gate_result_dict_unchanged_after_build(self) -> None:
        gate = _make_gate_result()
        original_gate_id = gate["gate_id"]
        original_keys = set(gate.keys())
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_gate_result(gate)
        builder.build()
        assert gate["gate_id"] == original_gate_id
        assert set(gate.keys()) == original_keys

    def test_receipt_dict_unchanged_after_build(self) -> None:
        receipt = _make_receipt()
        original_receipt_id = receipt["receipt_id"]
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_receipt(receipt)
        builder.build()
        assert receipt["receipt_id"] == original_receipt_id

    def test_runtime_event_dict_unchanged_after_build(self) -> None:
        event = _make_runtime_event()
        original_event_type = event["event_type"]
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_runtime_event(event)
        builder.build()
        assert event["event_type"] == original_event_type

    def test_payload_is_copy_not_reference(self) -> None:
        """Mutating source dict after add does not affect bundle payload."""
        gate = {"gate_id": "g-001", "outcome": "pass", "timestamp": "2026-01-01T00:00:00+00:00", "dispatch_id": "d-001"}
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_gate_result(gate)
        bundle = builder.build()
        gate["outcome"] = "MUTATED"  # mutate source after build
        gate_entry = next(e for e in bundle.evidence if e.evidence_type == EvidenceType.GATE_RESULT)
        assert gate_entry.payload["outcome"] == "pass"


# ---------------------------------------------------------------------------
# 4. Empty bundle rejected
# ---------------------------------------------------------------------------

class TestEmptyBundle:

    def test_empty_bundle_raises_empty_bundle_error(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(EmptyBundleError):
            builder.build()

    def test_empty_bundle_error_is_audit_bundle_error(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(AuditBundleError):
            builder.build()

    def test_empty_bundle_error_mentions_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-SPECIFIC-001")
        with pytest.raises(EmptyBundleError, match="d-SPECIFIC-001"):
            builder.build()


# ---------------------------------------------------------------------------
# 5. to_dict() is JSON-serializable
# ---------------------------------------------------------------------------

class TestToDict:

    def test_bundle_to_dict_is_json_serializable(self) -> None:
        bundle = _complete_builder().build()
        data = bundle.to_dict()
        # Must not raise — all values are primitive types
        serialized = json.dumps(data)
        assert isinstance(serialized, str)

    def test_bundle_to_dict_structure(self) -> None:
        bundle = _complete_builder().build()
        data = bundle.to_dict()
        assert "bundle_id" in data
        assert "dispatch_id" in data
        assert "created_at" in data
        assert "is_complete" in data
        assert "evidence_count" in data
        assert "evidence" in data

    def test_bundle_to_dict_evidence_is_list(self) -> None:
        bundle = _complete_builder().build()
        data = bundle.to_dict()
        assert isinstance(data["evidence"], list)

    def test_entry_to_dict_structure(self) -> None:
        bundle = _complete_builder().build()
        entry_dict = bundle.to_dict()["evidence"][0]
        assert "entry_id" in entry_dict
        assert "evidence_type" in entry_dict
        assert "timestamp" in entry_dict
        assert "payload" in entry_dict

    def test_entry_evidence_type_is_string(self) -> None:
        bundle = _complete_builder().build()
        for entry_dict in bundle.to_dict()["evidence"]:
            assert isinstance(entry_dict["evidence_type"], str)

    def test_bundle_to_dict_no_enum_values(self) -> None:
        """to_dict must not contain Enum instances — only primitives."""
        bundle = _complete_builder().build()
        serialized = json.dumps(bundle.to_dict())
        # If json.dumps succeeds without error, all values are primitives
        data = json.loads(serialized)
        assert data["bundle_id"].startswith("bundle-")

    def test_evidence_count_matches_list_length(self) -> None:
        bundle = _complete_builder().build()
        data = bundle.to_dict()
        assert data["evidence_count"] == len(data["evidence"])


# ---------------------------------------------------------------------------
# 6. Bundle completeness check
# ---------------------------------------------------------------------------

class TestCompleteness:

    def test_complete_bundle_is_complete(self) -> None:
        bundle = _complete_builder().build()
        assert bundle.is_complete() is True

    def test_bundle_with_approval_closure_gate_is_complete(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_gate_result(_make_gate_result())
        bundle = builder.build()
        assert bundle.is_complete() is True

    def test_bundle_with_approval_closure_receipt_is_complete(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_receipt(_make_receipt())
        bundle = builder.build()
        assert bundle.is_complete() is True

    def test_bundle_missing_approval_is_not_complete(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_closure(_make_closure())
        builder.add_gate_result(_make_gate_result())
        bundle = builder.build()
        assert bundle.is_complete() is False

    def test_bundle_missing_closure_is_not_complete(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_gate_result(_make_gate_result())
        bundle = builder.build()
        assert bundle.is_complete() is False

    def test_bundle_missing_gate_and_receipt_is_not_complete(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        bundle = builder.build()
        assert bundle.is_complete() is False

    def test_runtime_event_alone_does_not_satisfy_completeness(self) -> None:
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_runtime_event(_make_runtime_event())
        bundle = builder.build()
        # runtime_event is not a gate_result or receipt
        assert bundle.is_complete() is False

    def test_to_dict_includes_is_complete(self) -> None:
        bundle = _complete_builder().build()
        data = bundle.to_dict()
        assert isinstance(data["is_complete"], bool)
        assert data["is_complete"] is True


# ---------------------------------------------------------------------------
# 7. EvidenceEntry fields
# ---------------------------------------------------------------------------

class TestEvidenceEntry:

    def test_entry_has_entry_id(self) -> None:
        bundle = _complete_builder().build()
        for entry in bundle.evidence:
            assert entry.entry_id.startswith("entry-")

    def test_entry_ids_are_unique(self) -> None:
        bundle = _complete_builder().build()
        ids = [e.entry_id for e in bundle.evidence]
        assert len(ids) == len(set(ids))

    def test_entry_has_evidence_type(self) -> None:
        bundle = _complete_builder().build()
        for entry in bundle.evidence:
            assert isinstance(entry.evidence_type, EvidenceType)

    def test_entry_has_timestamp(self) -> None:
        bundle = _complete_builder().build()
        for entry in bundle.evidence:
            assert isinstance(entry.timestamp, str)
            assert len(entry.timestamp) > 0

    def test_entry_has_payload(self) -> None:
        bundle = _complete_builder().build()
        for entry in bundle.evidence:
            # payload is a MappingProxyType (read-only mapping) after AB-1 freeze
            assert hasattr(entry.payload, "__getitem__") and hasattr(entry.payload, "keys")

    def test_entry_id_format_is_entry_uuid(self) -> None:
        bundle = _complete_builder().build()
        for entry in bundle.evidence:
            # "entry-" is 6 chars; uuid4 is 36 chars
            assert len(entry.entry_id) == len("entry-") + 36


# ---------------------------------------------------------------------------
# 8. EvidenceType enum
# ---------------------------------------------------------------------------

class TestEvidenceTypeEnum:

    def test_approval_record_value(self) -> None:
        assert EvidenceType.APPROVAL_RECORD.value == "approval_record"

    def test_closure_record_value(self) -> None:
        assert EvidenceType.CLOSURE_RECORD.value == "closure_record"

    def test_gate_result_value(self) -> None:
        assert EvidenceType.GATE_RESULT.value == "gate_result"

    def test_receipt_value(self) -> None:
        assert EvidenceType.RECEIPT.value == "receipt"

    def test_runtime_event_value(self) -> None:
        assert EvidenceType.RUNTIME_EVENT.value == "runtime_event"

    def test_all_five_types_defined(self) -> None:
        assert len(EvidenceType) == 5


# ---------------------------------------------------------------------------
# 9. AuditBundleBuilder
# ---------------------------------------------------------------------------

class TestAuditBundleBuilder:

    def test_builder_starts_empty(self) -> None:
        builder = audit_bundle_builder("d-001")
        assert len(builder._entries) == 0

    def test_add_approval_returns_self(self) -> None:
        builder = audit_bundle_builder("d-001")
        result = builder.add_approval(_make_approval())
        assert result is builder

    def test_add_closure_returns_self(self) -> None:
        builder = audit_bundle_builder("d-001")
        result = builder.add_closure(_make_closure())
        assert result is builder

    def test_add_gate_result_returns_self(self) -> None:
        builder = audit_bundle_builder("d-001")
        result = builder.add_gate_result(_make_gate_result())
        assert result is builder

    def test_add_receipt_returns_self(self) -> None:
        builder = audit_bundle_builder("d-001")
        result = builder.add_receipt(_make_receipt())
        assert result is builder

    def test_add_runtime_event_returns_self(self) -> None:
        builder = audit_bundle_builder("d-001")
        result = builder.add_runtime_event(_make_runtime_event())
        assert result is builder

    def test_method_chaining(self) -> None:
        bundle = (
            audit_bundle_builder("d-001")
            .add_approval(_make_approval())
            .add_closure(_make_closure())
            .add_gate_result(_make_gate_result())
            .add_receipt(_make_receipt())
            .add_runtime_event(_make_runtime_event())
            .build()
        )
        assert len(bundle.evidence) == 5

    def test_add_approval_wrong_type_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError):
            builder.add_approval({"not": "an_approval_record"})  # type: ignore[arg-type]

    def test_add_closure_wrong_type_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError):
            builder.add_closure("not a closure record")  # type: ignore[arg-type]

    def test_add_gate_result_missing_gate_id_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="gate_id"):
            builder.add_gate_result({"outcome": "pass", "timestamp": "2026-01-01T00:00:00+00:00"})

    def test_add_gate_result_missing_outcome_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="outcome"):
            builder.add_gate_result({"gate_id": "g-001", "timestamp": "2026-01-01T00:00:00+00:00"})

    def test_add_gate_result_missing_timestamp_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="timestamp"):
            builder.add_gate_result({"gate_id": "g-001", "outcome": "pass"})

    def test_add_receipt_missing_receipt_id_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="receipt_id"):
            builder.add_receipt({"dispatch_id": "d-001", "timestamp": "2026-01-01T00:00:00+00:00"})

    def test_add_receipt_missing_dispatch_id_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="dispatch_id"):
            builder.add_receipt({"receipt_id": "r-001", "timestamp": "2026-01-01T00:00:00+00:00"})

    def test_add_runtime_event_missing_event_type_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="event_type"):
            builder.add_runtime_event({"timestamp": "2026-01-01T00:00:00+00:00"})

    def test_add_runtime_event_missing_timestamp_raises(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="timestamp"):
            builder.add_runtime_event({"event_type": "session_start"})

    def test_runtime_event_extra_fields_preserved(self) -> None:
        builder = audit_bundle_builder("d-001")
        event = {
            "event_type": "session_start",
            "timestamp": "2026-04-03T15:00:00+00:00",
            "session_id": "sess-abc",
            "worker_id": "T2",
            "dispatch_id": "d-001",
        }
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_runtime_event(event)
        bundle = builder.build()
        entry = next(e for e in bundle.evidence if e.evidence_type == EvidenceType.RUNTIME_EVENT)
        assert entry.payload["session_id"] == "sess-abc"
        assert entry.payload["worker_id"] == "T2"


# ---------------------------------------------------------------------------
# 10. AuditBundle immutability
# ---------------------------------------------------------------------------

class TestAuditBundleImmutability:

    def test_bundle_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        with pytest.raises(Exception):
            bundle.bundle_id = "modified"  # type: ignore[misc]

    def test_bundle_dispatch_id_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        with pytest.raises(Exception):
            bundle.dispatch_id = "d-999"  # type: ignore[misc]

    def test_bundle_evidence_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        with pytest.raises(Exception):
            bundle.evidence = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 11. EvidenceEntry immutability
# ---------------------------------------------------------------------------

class TestEvidenceEntryImmutability:

    def test_entry_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        entry = bundle.evidence[0]
        with pytest.raises(Exception):
            entry.entry_id = "modified"  # type: ignore[misc]

    def test_entry_evidence_type_is_frozen(self) -> None:
        bundle = _complete_builder().build()
        entry = bundle.evidence[0]
        with pytest.raises(Exception):
            entry.evidence_type = EvidenceType.RECEIPT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 12. Field validation — InvalidEvidenceError
# ---------------------------------------------------------------------------

class TestFieldValidation:

    def test_invalid_evidence_error_is_audit_bundle_error(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(AuditBundleError):
            builder.add_gate_result({})

    def test_error_message_lists_missing_fields(self) -> None:
        builder = audit_bundle_builder("d-001")
        with pytest.raises(InvalidEvidenceError, match="gate_id"):
            builder.add_gate_result({})


# ---------------------------------------------------------------------------
# 13. Factory — audit_bundle_builder()
# ---------------------------------------------------------------------------

class TestFactory:

    def test_factory_returns_builder(self) -> None:
        builder = audit_bundle_builder("d-001")
        assert isinstance(builder, AuditBundleBuilder)

    def test_factory_sets_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-test-999")
        assert builder.dispatch_id == "d-test-999"

    def test_factory_empty_dispatch_id_raises(self) -> None:
        with pytest.raises(ValueError, match="dispatch_id"):
            audit_bundle_builder("")

    def test_factory_whitespace_dispatch_id_raises(self) -> None:
        with pytest.raises(ValueError):
            audit_bundle_builder("   ")

    def test_factory_fresh_builder_has_no_entries(self) -> None:
        builder = audit_bundle_builder("d-001")
        assert len(builder._entries) == 0


# ---------------------------------------------------------------------------
# Integration: full regulated_strict dispatch lifecycle
# ---------------------------------------------------------------------------

class TestIntegrationLifecycle:
    """Simulate a complete regulated_strict dispatch run as T0 would construct it."""

    def test_full_lifecycle_bundle(self) -> None:
        dispatch_id = "d-lifecycle-001"
        policy = _policy()

        approval = policy.record_approval(
            dispatch_id=dispatch_id,
            approved_by="operator",
            rationale="Reviewed gate evidence; all checks green. Approved for execution.",
            approval_type=ApprovalType.PRE_EXECUTION,
            evidence_refs=["gate_pr2_audit_bundle_builder"],
        )
        closure = policy.record_closure(
            dispatch_id=dispatch_id,
            closed_by="operator",
            rationale="Post-review complete. All tests passed. No residual risks.",
            closure_type=ClosureType.APPROVED,
            bundle_complete=True,
            open_items_resolved=True,
        )
        gate = {"gate_id": "gate_pr2_audit_bundle_builder", "outcome": "pass", "timestamp": "2026-04-03T16:00:00+00:00", "dispatch_id": dispatch_id}
        receipt = {"receipt_id": "rcpt-001", "dispatch_id": dispatch_id, "timestamp": "2026-04-03T16:01:00+00:00"}
        event = {"event_type": "session_complete", "timestamp": "2026-04-03T16:02:00+00:00", "duration_s": 45, "dispatch_id": dispatch_id}

        bundle = (
            audit_bundle_builder(dispatch_id)
            .add_approval(approval)
            .add_closure(closure)
            .add_gate_result(gate)
            .add_receipt(receipt)
            .add_runtime_event(event)
            .build()
        )

        assert bundle.bundle_id.startswith("bundle-")
        assert bundle.dispatch_id == dispatch_id
        assert bundle.is_complete() is True
        assert len(bundle.evidence) == 5

        # Fully serializable
        data = bundle.to_dict()
        json_str = json.dumps(data)
        reconstructed = json.loads(json_str)
        assert reconstructed["bundle_id"] == bundle.bundle_id
        assert reconstructed["dispatch_id"] == dispatch_id
        assert reconstructed["is_complete"] is True
        assert reconstructed["evidence_count"] == 5

    def test_approval_payload_survives_serialization(self) -> None:
        """Approval record fields must be present in bundle payload after round-trip."""
        dispatch_id = "d-serial-001"
        approval = _make_approval(dispatch_id)
        builder = audit_bundle_builder(dispatch_id)
        builder.add_approval(approval)
        builder.add_closure(_make_closure(dispatch_id))
        builder.add_gate_result(_make_gate_result(dispatch_id=dispatch_id))
        bundle = builder.build()
        data = json.loads(json.dumps(bundle.to_dict()))
        approval_entries = [
            e for e in data["evidence"]
            if e["evidence_type"] == "approval_record"
        ]
        assert len(approval_entries) == 1
        payload = approval_entries[0]["payload"]
        assert payload["approval_id"] == approval.approval_id
        assert payload["dispatch_id"] == dispatch_id
        assert payload["approved_by"] == "operator"


# ---------------------------------------------------------------------------
# AB-1 payload immutability: mutation via payload reference is blocked
# ---------------------------------------------------------------------------

class TestPayloadImmutability:

    def test_payload_is_read_only_after_entry_creation(self) -> None:
        """Payload stored in EvidenceEntry is a MappingProxyType — write raises TypeError."""
        import types as _types
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_gate_result(_make_gate_result())
        bundle = builder.build()
        gate_entry = next(
            e for e in bundle.evidence if e.evidence_type == EvidenceType.GATE_RESULT
        )
        assert isinstance(gate_entry.payload, _types.MappingProxyType)
        with pytest.raises(TypeError):
            gate_entry.payload["outcome"] = "MUTATED"  # type: ignore[index]

    def test_original_dict_mutation_does_not_affect_stored_payload(self) -> None:
        """Mutating the source dict after adding to builder does not affect the entry."""
        source = _make_gate_result()
        builder = audit_bundle_builder("d-001")
        builder.add_approval(_make_approval())
        builder.add_closure(_make_closure())
        builder.add_gate_result(source)
        source["outcome"] = "MUTATED"  # mutate after add
        bundle = builder.build()
        gate_entry = next(
            e for e in bundle.evidence if e.evidence_type == EvidenceType.GATE_RESULT
        )
        assert gate_entry.payload["outcome"] == "pass"
