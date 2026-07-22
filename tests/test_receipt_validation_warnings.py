#!/usr/bin/env python3
"""Tests for the ADR-035 §6.1/§3.2.1 warnings[]/event_type reject-list
extension in scripts/lib/append_receipt_internals/validation.py::_validate_receipt.

Covers the PR-2 mandatory subset: T6, T31-T33, T36-T38, plus the
event-name format-guard checks §3.2.1/§6.1 requires alongside T38's
schema_version gating.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.validation import _validate_receipt  # noqa: E402
from append_receipt_internals.warning_destination import (  # noqa: E402
    DROP_REASON_ALLOWLIST,
)


def _receipt(
    *,
    event_type: Optional[str] = "task_complete",
    dispatch_id: str = "DISP-TEST-001",
    warnings: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {
        "timestamp": "2026-07-22T10:00:00Z",
        "dispatch_id": dispatch_id,
    }
    if event_type is not None:
        receipt["event_type"] = event_type
    if warnings is not None:
        receipt["warnings"] = warnings
    receipt.update(extra)
    return receipt


def _tracked_blocker(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "code": "worker_permission_violation",
        "severity": "blocker",
        "message": "worker wrote outside its declared file-write scope",
        "destination": "oi",
        "oi_id": "OI-001",
        "reason": None,
        "requires_tracking": True,
    }
    entry.update(overrides)
    return entry


def _counted_warn(**overrides: Any) -> Dict[str, Any]:
    entry = {
        "code": "report_contract_invalid",
        "severity": "warn",
        "message": "Summary section missing",
        "destination": "counted",
        "oi_id": None,
        "reason": None,
        "requires_tracking": False,
    }
    entry.update(overrides)
    return entry


# ── T6 — dropped + reason:null is rejected ─────────────────────────────────


def test_t6_dropped_with_null_reason_rejected():
    entry = _counted_warn(destination="dropped", reason=None)
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


def test_t6_valid_receipt_with_no_warnings_still_validates():
    _validate_receipt(_receipt())  # must not raise


# ── T31 — oi_pending legal only with oi_id:null + non-null reason ─────────


def test_t31_oi_pending_legal_shape_accepted():
    entry = _tracked_blocker(destination="oi_pending", oi_id=None, reason="store lock held")
    _validate_receipt(_receipt(warnings=[entry]))  # must not raise


def test_t31_oi_pending_with_non_null_oi_id_rejected():
    entry = _tracked_blocker(destination="oi_pending", oi_id="OI-001", reason="store lock held")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


def test_t31_oi_pending_with_null_reason_rejected():
    entry = _tracked_blocker(destination="oi_pending", oi_id=None, reason=None)
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


# ── T32 — requires_tracking:true paired with counted/dropped is rejected ──


@pytest.mark.parametrize("destination", ["counted", "dropped"])
def test_t32_requires_tracking_true_with_counted_or_dropped_rejected(destination):
    entry = _tracked_blocker(destination=destination, oi_id=None)
    if destination == "dropped":
        entry["reason"] = "retired_check"
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


def test_t32_requires_tracking_true_with_oi_or_oi_pending_accepted():
    _validate_receipt(_receipt(warnings=[_tracked_blocker()]))
    entry = _tracked_blocker(destination="oi_pending", oi_id=None, reason="pending")
    _validate_receipt(_receipt(warnings=[entry]))


# ── T33 — dropped reason must come from the closed allow-list ─────────────


def test_t33_dropped_reason_outside_allowlist_rejected():
    entry = _counted_warn(destination="dropped", reason="meh, not important")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


@pytest.mark.parametrize("reason", sorted(DROP_REASON_ALLOWLIST))
def test_t33_dropped_reason_in_allowlist_accepted(reason):
    entry = _counted_warn(destination="dropped", reason=reason)
    _validate_receipt(_receipt(warnings=[entry]))  # must not raise


# ── T36 — severity outside {blocker, warn, info} is rejected ──────────────


def test_t36_severity_critical_rejected():
    entry = _tracked_blocker(severity="critical")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


@pytest.mark.parametrize("severity", ["blocker", "warn", "info"])
def test_t36_legal_severities_accepted(severity):
    entry = _counted_warn(severity=severity, requires_tracking=(severity == "blocker"))
    if severity == "blocker":
        entry["destination"] = "oi"
        entry["oi_id"] = "OI-002"
    _validate_receipt(_receipt(warnings=[entry]))  # must not raise


# ── T37 — severity:blocker + requires_tracking:false rejected regardless ──
# ── of destination (incl. an otherwise-legal "counted")                  ──


def test_t37_blocker_requires_tracking_false_rejected_with_counted_destination():
    """The exact r3 HIGH-3 scenario: {severity: blocker, requires_tracking:
    false, destination: counted} — legal under check 1 alone, rejected only
    by check 2 (severity => requires_tracking)."""
    entry = _counted_warn(severity="blocker", requires_tracking=False, destination="counted")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


def test_t37_blocker_requires_tracking_false_rejected_with_oi_destination():
    entry = _tracked_blocker(requires_tracking=False)
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(warnings=[entry]))


# ── T38 — event/event_type alias boundary is schema_version-gated ────────


def test_t38_schema_version_2_with_event_alias_and_no_event_type_rejected():
    receipt = _receipt(event_type=None, schema_version=2, event="task_complete")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(receipt)


def test_t38_schema_version_absent_same_shape_still_validates_via_alias():
    receipt = _receipt(event_type=None, event="task_complete")
    _validate_receipt(receipt)  # must not raise


def test_t38_schema_version_1_same_shape_still_validates_via_alias():
    receipt = _receipt(event_type=None, schema_version=1, event="task_complete")
    _validate_receipt(receipt)  # must not raise


def test_t38_schema_version_2_with_event_type_present_validates():
    receipt = _receipt(event_type="task_complete", schema_version=2)
    _validate_receipt(receipt)  # must not raise


def test_t38_schema_version_2_with_empty_event_type_and_event_alias_rejected():
    receipt = _receipt(event_type="", schema_version=2, event="task_complete")
    with pytest.raises(AppendReceiptError):
        _validate_receipt(receipt)


# ── event-name format guard — typo/garbage guard, never a membership test ─


def test_event_name_format_guard_rejects_camelcase():
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(event_type="TaskComplete"))


def test_event_name_format_guard_rejects_embedded_whitespace():
    with pytest.raises(AppendReceiptError):
        _validate_receipt(_receipt(event_type="task complete"))


def test_event_name_format_guard_accepts_never_before_seen_snake_case_value():
    """Proves the guard is required-and-non-empty + format, never a closed
    enum (r3 BLOCKING-1) — a brand-new event_type value is accepted."""
    _validate_receipt(_receipt(event_type="a_new_gate_event_2026"))  # must not raise
