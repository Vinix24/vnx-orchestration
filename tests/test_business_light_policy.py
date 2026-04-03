#!/usr/bin/env python3
"""Tests for business-light review and closeout policy (Feature 20, PR-2).

Covers:
  1. ReviewMode              — enum values
  2. OpenItemSeverity        — enum values
  3. AuditArtifactType       — enum values
  4. OpenItem                — construction, is_blocking behavior
  5. AuditArtifact           — construction, to_dict, immutability
  6. AuditRecord             — find_by_type, is_empty, with_artifact (immutable)
  7. GateResult              — structure, is_blocked
  8. CloseoutDecision        — explicit invariant, ValueError on is_explicit=False
  9. BusinessLightReviewPolicy — can_proceed, gate_result, apply_closeout
  10. Review-by-exception gating — severity-based blocking semantics
  11. Audit retention          — artifacts persist through all operations
  12. No auto-closeout         — is_explicit=False rejected at construction
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from business_light_policy import (
    AuditArtifact,
    AuditArtifactType,
    AuditRecord,
    BusinessLightReviewPolicy,
    CloseoutDecision,
    GateResult,
    OpenItem,
    OpenItemSeverity,
    ReviewMode,
    business_light_policy,
)


# ---------------------------------------------------------------------------
# 1. ReviewMode
# ---------------------------------------------------------------------------

class TestReviewMode:

    def test_full_review_value(self) -> None:
        assert ReviewMode.FULL_REVIEW.value == "full_review"

    def test_review_by_exception_value(self) -> None:
        assert ReviewMode.REVIEW_BY_EXCEPTION.value == "review_by_exception"

    def test_modes_are_distinct(self) -> None:
        assert ReviewMode.FULL_REVIEW != ReviewMode.REVIEW_BY_EXCEPTION


# ---------------------------------------------------------------------------
# 2. OpenItemSeverity
# ---------------------------------------------------------------------------

class TestOpenItemSeverity:

    def test_blocker_value(self) -> None:
        assert OpenItemSeverity.BLOCKER.value == "blocker"

    def test_warning_value(self) -> None:
        assert OpenItemSeverity.WARNING.value == "warning"

    def test_info_value(self) -> None:
        assert OpenItemSeverity.INFO.value == "info"

    def test_severities_distinct(self) -> None:
        assert OpenItemSeverity.BLOCKER != OpenItemSeverity.WARNING
        assert OpenItemSeverity.WARNING != OpenItemSeverity.INFO


# ---------------------------------------------------------------------------
# 3. AuditArtifactType
# ---------------------------------------------------------------------------

class TestAuditArtifactType:

    def test_all_types_defined(self) -> None:
        values = {t.value for t in AuditArtifactType}
        assert "review_decision" in values
        assert "open_item" in values
        assert "closeout_decision" in values
        assert "finding" in values
        assert "retention_receipt" in values


# ---------------------------------------------------------------------------
# 4. OpenItem
# ---------------------------------------------------------------------------

class TestOpenItem:

    def test_construction(self) -> None:
        item = OpenItem("i1", "missing doc", OpenItemSeverity.WARNING)
        assert item.item_id == "i1"
        assert item.description == "missing doc"
        assert item.severity == OpenItemSeverity.WARNING

    def test_blocker_is_blocking_by_default(self) -> None:
        item = OpenItem("i1", "sec gap", OpenItemSeverity.BLOCKER)
        assert item.is_blocking() is True

    def test_warning_is_not_blocking(self) -> None:
        item = OpenItem("i1", "doc missing", OpenItemSeverity.WARNING)
        assert item.is_blocking() is False

    def test_info_is_not_blocking(self) -> None:
        item = OpenItem("i1", "note", OpenItemSeverity.INFO)
        assert item.is_blocking() is False

    def test_blocker_resolved_is_not_blocking(self) -> None:
        item = OpenItem("i1", "sec gap", OpenItemSeverity.BLOCKER, is_resolved=True)
        assert item.is_blocking() is False

    def test_blocker_no_resolution_required_is_not_blocking(self) -> None:
        item = OpenItem("i1", "sec gap", OpenItemSeverity.BLOCKER,
                        requires_resolution=False)
        assert item.is_blocking() is False

    def test_defaults_require_resolution_true(self) -> None:
        item = OpenItem("i1", "x", OpenItemSeverity.BLOCKER)
        assert item.requires_resolution is True
        assert item.is_resolved is False

    def test_open_item_is_frozen(self) -> None:
        item = OpenItem("i1", "x", OpenItemSeverity.BLOCKER)
        with pytest.raises(Exception):
            item.is_resolved = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. AuditArtifact
# ---------------------------------------------------------------------------

class TestAuditArtifact:

    def test_construction(self) -> None:
        a = AuditArtifact("a1", AuditArtifactType.FINDING, note="ok")
        assert a.artifact_id == "a1"
        assert a.artifact_type == AuditArtifactType.FINDING
        assert a.note == "ok"

    def test_note_default_empty(self) -> None:
        a = AuditArtifact("a1", AuditArtifactType.FINDING)
        assert a.note == ""

    def test_to_dict_structure(self) -> None:
        a = AuditArtifact("a1", AuditArtifactType.OPEN_ITEM, note="retained")
        d = a.to_dict()
        assert d["artifact_id"] == "a1"
        assert d["artifact_type"] == "open_item"
        assert d["note"] == "retained"

    def test_artifact_is_frozen(self) -> None:
        a = AuditArtifact("a1", AuditArtifactType.FINDING)
        with pytest.raises(Exception):
            a.note = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. AuditRecord
# ---------------------------------------------------------------------------

class TestAuditRecord:

    def _record(self) -> AuditRecord:
        return AuditRecord(task_id="d-001")

    def test_task_id_preserved(self) -> None:
        assert self._record().task_id == "d-001"

    def test_default_is_empty(self) -> None:
        assert self._record().is_empty() is True

    def test_with_artifact_returns_new_record(self) -> None:
        r1 = self._record()
        a = AuditArtifact("a1", AuditArtifactType.FINDING)
        r2 = r1.with_artifact(a)
        assert r2 is not r1

    def test_with_artifact_does_not_mutate_original(self) -> None:
        r1 = self._record()
        a = AuditArtifact("a1", AuditArtifactType.FINDING)
        r1.with_artifact(a)
        assert r1.is_empty()  # original unchanged

    def test_with_artifact_appends(self) -> None:
        r = self._record()
        r = r.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        assert len(r.artifacts) == 1

    def test_multiple_artifacts_retained(self) -> None:
        r = self._record()
        r = r.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        r = r.with_artifact(AuditArtifact("a2", AuditArtifactType.OPEN_ITEM))
        assert len(r.artifacts) == 2

    def test_find_by_type_returns_matching(self) -> None:
        r = self._record()
        r = r.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        r = r.with_artifact(AuditArtifact("a2", AuditArtifactType.OPEN_ITEM))
        findings = r.find_by_type(AuditArtifactType.FINDING)
        assert len(findings) == 1
        assert findings[0].artifact_id == "a1"

    def test_find_by_type_returns_empty_when_none(self) -> None:
        r = self._record()
        assert r.find_by_type(AuditArtifactType.CLOSEOUT_DECISION) == []

    def test_is_empty_false_after_artifact(self) -> None:
        r = self._record().with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        assert r.is_empty() is False

    def test_record_is_frozen(self) -> None:
        r = self._record()
        with pytest.raises(Exception):
            r.task_id = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. GateResult
# ---------------------------------------------------------------------------

class TestGateResult:

    def _record(self) -> AuditRecord:
        return AuditRecord(task_id="d-001")

    def test_passed_true_when_no_blocking(self) -> None:
        gr = GateResult(passed=True, blocking_items=(), record=self._record())
        assert gr.passed is True
        assert gr.is_blocked() is False

    def test_passed_false_when_blocking(self) -> None:
        item = OpenItem("i1", "gap", OpenItemSeverity.BLOCKER)
        gr = GateResult(passed=False, blocking_items=(item,), record=self._record())
        assert gr.passed is False
        assert gr.is_blocked() is True

    def test_blocking_items_accessible(self) -> None:
        item = OpenItem("i1", "gap", OpenItemSeverity.BLOCKER)
        gr = GateResult(passed=False, blocking_items=(item,), record=self._record())
        assert len(gr.blocking_items) == 1
        assert gr.blocking_items[0].item_id == "i1"

    def test_record_retained_in_gate_result(self) -> None:
        r = self._record().with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        gr = GateResult(passed=True, blocking_items=(), record=r)
        assert not gr.record.is_empty()


# ---------------------------------------------------------------------------
# 8. CloseoutDecision
# ---------------------------------------------------------------------------

class TestCloseoutDecision:

    def test_explicit_default_true(self) -> None:
        d = CloseoutDecision(task_id="d-001", decided_by="T0")
        assert d.is_explicit is True

    def test_decided_by_preserved(self) -> None:
        d = CloseoutDecision(task_id="d-001", decided_by="manager-T0")
        assert d.decided_by == "manager-T0"

    def test_task_id_preserved(self) -> None:
        d = CloseoutDecision(task_id="d-042", decided_by="T0")
        assert d.task_id == "d-042"

    def test_is_explicit_false_raises(self) -> None:
        with pytest.raises(ValueError, match="is_explicit"):
            CloseoutDecision(task_id="d-001", decided_by="T0", is_explicit=False)

    def test_error_message_mentions_auto_closeout(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            CloseoutDecision(task_id="d-001", decided_by="T0", is_explicit=False)
        assert "Automatic" in str(exc_info.value)

    def test_decision_is_frozen(self) -> None:
        d = CloseoutDecision(task_id="d-001", decided_by="T0")
        with pytest.raises(Exception):
            d.decided_by = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 9. BusinessLightReviewPolicy
# ---------------------------------------------------------------------------

class TestBusinessLightReviewPolicy:

    def setup_method(self) -> None:
        self.policy = business_light_policy()
        self.record = AuditRecord(task_id="d-001")

    def test_review_mode_is_review_by_exception(self) -> None:
        assert self.policy.review_mode == ReviewMode.REVIEW_BY_EXCEPTION

    def test_can_proceed_no_items(self) -> None:
        assert self.policy.can_proceed([]) is True

    def test_can_proceed_only_warnings(self) -> None:
        items = [OpenItem("i1", "doc", OpenItemSeverity.WARNING)]
        assert self.policy.can_proceed(items) is True

    def test_can_proceed_only_info(self) -> None:
        items = [OpenItem("i1", "note", OpenItemSeverity.INFO)]
        assert self.policy.can_proceed(items) is True

    def test_can_proceed_blocker_blocks(self) -> None:
        items = [OpenItem("i1", "gap", OpenItemSeverity.BLOCKER)]
        assert self.policy.can_proceed(items) is False

    def test_gate_result_passes_with_no_blockers(self) -> None:
        gr = self.policy.gate_result([], self.record)
        assert gr.passed is True
        assert len(gr.blocking_items) == 0

    def test_gate_result_blocked_by_blocker(self) -> None:
        items = [OpenItem("i1", "gap", OpenItemSeverity.BLOCKER)]
        gr = self.policy.gate_result(items, self.record)
        assert gr.passed is False
        assert len(gr.blocking_items) == 1

    def test_gate_result_record_retained(self) -> None:
        record = self.record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        gr = self.policy.gate_result([], record)
        assert not gr.record.is_empty()

    def test_apply_closeout_appends_artifact(self) -> None:
        decision = CloseoutDecision(task_id="d-001", decided_by="T0")
        updated = self.policy.apply_closeout(decision, self.record)
        closeouts = updated.find_by_type(AuditArtifactType.CLOSEOUT_DECISION)
        assert len(closeouts) == 1

    def test_apply_closeout_preserves_existing_artifacts(self) -> None:
        record = self.record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        decision = CloseoutDecision(task_id="d-001", decided_by="T0")
        updated = self.policy.apply_closeout(decision, record)
        assert len(updated.find_by_type(AuditArtifactType.FINDING)) == 1
        assert len(updated.find_by_type(AuditArtifactType.CLOSEOUT_DECISION)) == 1

    def test_apply_closeout_note_includes_decided_by(self) -> None:
        decision = CloseoutDecision(task_id="d-001", decided_by="manager-T0")
        updated = self.policy.apply_closeout(decision, self.record)
        closeout = updated.find_by_type(AuditArtifactType.CLOSEOUT_DECISION)[0]
        assert "manager-T0" in closeout.note

    def test_policy_is_frozen(self) -> None:
        with pytest.raises(Exception):
            self.policy.review_mode = ReviewMode.FULL_REVIEW  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 10. Review-by-exception gating semantics
# ---------------------------------------------------------------------------

class TestReviewByExceptionGating:

    def setup_method(self) -> None:
        self.policy = business_light_policy()

    def test_warning_does_not_block_progress(self) -> None:
        items = [
            OpenItem("w1", "doc gap", OpenItemSeverity.WARNING),
            OpenItem("w2", "minor style", OpenItemSeverity.WARNING),
        ]
        assert self.policy.can_proceed(items) is True

    def test_info_does_not_block_progress(self) -> None:
        items = [OpenItem("n1", "fyi", OpenItemSeverity.INFO)]
        assert self.policy.can_proceed(items) is True

    def test_single_blocker_blocks_all(self) -> None:
        items = [
            OpenItem("w1", "doc gap", OpenItemSeverity.WARNING),
            OpenItem("b1", "sec issue", OpenItemSeverity.BLOCKER),
        ]
        assert self.policy.can_proceed(items) is False

    def test_resolved_blocker_does_not_block(self) -> None:
        items = [OpenItem("b1", "fixed gap", OpenItemSeverity.BLOCKER, is_resolved=True)]
        assert self.policy.can_proceed(items) is True

    def test_gate_captures_only_blockers_in_blocking_items(self) -> None:
        items = [
            OpenItem("w1", "doc", OpenItemSeverity.WARNING),
            OpenItem("b1", "gap", OpenItemSeverity.BLOCKER),
            OpenItem("i1", "note", OpenItemSeverity.INFO),
        ]
        gr = self.policy.gate_result(items, AuditRecord(task_id="d-001"))
        assert len(gr.blocking_items) == 1
        assert gr.blocking_items[0].item_id == "b1"

    def test_multiple_blockers_all_captured(self) -> None:
        items = [
            OpenItem("b1", "gap1", OpenItemSeverity.BLOCKER),
            OpenItem("b2", "gap2", OpenItemSeverity.BLOCKER),
        ]
        gr = self.policy.gate_result(items, AuditRecord(task_id="d-001"))
        assert len(gr.blocking_items) == 2


# ---------------------------------------------------------------------------
# 11. Audit retention
# ---------------------------------------------------------------------------

class TestAuditRetention:

    def setup_method(self) -> None:
        self.policy = business_light_policy()

    def test_artifacts_retained_after_can_proceed(self) -> None:
        record = AuditRecord(task_id="d-001")
        record = record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING,
                                                      note="checks passed"))
        # can_proceed doesn't touch the record
        assert self.policy.can_proceed([]) is True
        assert not record.is_empty()

    def test_warning_items_retained_in_record(self) -> None:
        record = AuditRecord(task_id="d-001")
        artifact = AuditArtifact("w1", AuditArtifactType.OPEN_ITEM, note="doc gap")
        record = record.with_artifact(artifact)
        assert not record.is_empty()
        assert len(record.find_by_type(AuditArtifactType.OPEN_ITEM)) == 1

    def test_artifacts_persist_through_closeout(self) -> None:
        record = AuditRecord(task_id="d-001")
        record = record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        record = record.with_artifact(AuditArtifact("a2", AuditArtifactType.OPEN_ITEM))
        decision = CloseoutDecision(task_id="d-001", decided_by="T0")
        record = self.policy.apply_closeout(decision, record)
        # Both original artifacts + closeout artifact retained
        assert len(record.artifacts) == 3

    def test_audit_record_immutable_across_operations(self) -> None:
        r0 = AuditRecord(task_id="d-001")
        r1 = r0.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        r2 = r1.with_artifact(AuditArtifact("a2", AuditArtifactType.OPEN_ITEM))
        assert r0.is_empty()
        assert len(r1.artifacts) == 1
        assert len(r2.artifacts) == 2

    def test_gate_result_includes_current_record(self) -> None:
        record = AuditRecord(task_id="d-001")
        record = record.with_artifact(AuditArtifact("a1", AuditArtifactType.FINDING))
        gr = self.policy.gate_result([], record)
        assert not gr.record.is_empty()
        assert len(gr.record.find_by_type(AuditArtifactType.FINDING)) == 1


# ---------------------------------------------------------------------------
# 12. No auto-closeout
# ---------------------------------------------------------------------------

class TestNoAutoCloseout:

    def test_is_explicit_false_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            CloseoutDecision(task_id="d-001", decided_by="T0", is_explicit=False)

    def test_auto_closeout_message_explicit(self) -> None:
        with pytest.raises(ValueError, match="Automatic"):
            CloseoutDecision(task_id="d-001", decided_by="T0", is_explicit=False)

    def test_only_explicit_closeout_accepted(self) -> None:
        """Verify the only valid path is is_explicit=True."""
        d = CloseoutDecision(task_id="d-001", decided_by="T0")
        assert d.is_explicit is True

    def test_policy_cannot_apply_implicit_closeout(self) -> None:
        """ValueError raised before apply_closeout is even called."""
        policy = business_light_policy()
        record = AuditRecord(task_id="d-001")
        with pytest.raises(ValueError):
            decision = CloseoutDecision(task_id="d-001", decided_by="T0",
                                        is_explicit=False)
            policy.apply_closeout(decision, record)

    def test_closeout_always_creates_audit_artifact(self) -> None:
        """Explicit closeout leaves an audit trail."""
        policy = business_light_policy()
        record = AuditRecord(task_id="d-001")
        decision = CloseoutDecision(task_id="d-001", decided_by="T0")
        updated = policy.apply_closeout(decision, record)
        assert len(updated.find_by_type(AuditArtifactType.CLOSEOUT_DECISION)) == 1
