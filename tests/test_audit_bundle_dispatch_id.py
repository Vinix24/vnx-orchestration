#!/usr/bin/env python3
"""Dispatch-ID guard tests for AuditBundleBuilder (Feature 17, PR-2).

Covers cross-dispatch evidence rejection in all add_* methods.
Core bundle tests are in test_audit_bundle.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from audit_bundle import audit_bundle_builder
from regulated_strict_approval import ApprovalType, regulated_strict_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy():
    return regulated_strict_policy()


def _make_approval(dispatch_id: str = "d-001"):
    return _policy().record_approval(
        dispatch_id=dispatch_id,
        approved_by="operator",
        rationale="Reviewed and approved for execution",
        approval_type=ApprovalType.PRE_EXECUTION,
    )


def _make_closure(dispatch_id: str = "d-001"):
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


# ---------------------------------------------------------------------------
# Dispatch-ID guard: cross-dispatch evidence rejected in all add_* methods
# ---------------------------------------------------------------------------

class TestDispatchIdGuard:

    def test_add_approval_rejects_wrong_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        foreign_approval = _make_approval(dispatch_id="d-other")
        with pytest.raises(ValueError, match="d-other"):
            builder.add_approval(foreign_approval)

    def test_add_approval_accepts_matching_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        builder.add_approval(_make_approval(dispatch_id="d-target"))
        assert len(builder._entries) == 1

    def test_add_closure_rejects_wrong_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        foreign_closure = _make_closure(dispatch_id="d-other")
        with pytest.raises(ValueError, match="d-other"):
            builder.add_closure(foreign_closure)

    def test_add_closure_accepts_matching_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        builder.add_closure(_make_closure(dispatch_id="d-target"))
        assert len(builder._entries) == 1

    def test_add_gate_result_rejects_wrong_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        with pytest.raises(ValueError, match="d-other"):
            builder.add_gate_result(_make_gate_result(dispatch_id="d-other"))

    def test_add_gate_result_accepts_matching_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        builder.add_gate_result(_make_gate_result(dispatch_id="d-target"))
        assert len(builder._entries) == 1

    def test_add_receipt_rejects_wrong_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        with pytest.raises(ValueError, match="d-other"):
            builder.add_receipt(_make_receipt(dispatch_id="d-other"))

    def test_add_receipt_accepts_matching_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        builder.add_receipt(_make_receipt(dispatch_id="d-target"))
        assert len(builder._entries) == 1

    def test_add_runtime_event_rejects_wrong_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        with pytest.raises(ValueError, match="d-other"):
            builder.add_runtime_event(_make_runtime_event(dispatch_id="d-other"))

    def test_add_runtime_event_accepts_matching_dispatch_id(self) -> None:
        builder = audit_bundle_builder("d-target")
        builder.add_runtime_event(_make_runtime_event(dispatch_id="d-target"))
        assert len(builder._entries) == 1

    def test_mismatch_error_includes_builder_dispatch_id(self) -> None:
        """Error message names the builder's dispatch_id so operator knows what was expected."""
        builder = audit_bundle_builder("d-expected")
        foreign_approval = _make_approval(dispatch_id="d-foreign")
        with pytest.raises(ValueError, match="d-expected"):
            builder.add_approval(foreign_approval)
