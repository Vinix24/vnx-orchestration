#!/usr/bin/env python3
"""Tests for the report_parser body-contract enforcement (audit governance #10).

Dispatch-ID: 20260627-audit-report-parser-contract

The receipt processor must not admit a success-claim with an invalid report body into the audit
trail as a clean task_complete. A success-claim + invalid body -> report_contract_invalid; a
contract-valid success report -> task_complete; a non-success report is never reclassified.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from report_parser import ReportParser  # noqa: E402

_VALID_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260627-valid

## Summary
This is a genuine completion report whose summary is comfortably longer than fifty non-whitespace
characters so the body contract validator accepts it.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""

_PHANTOM_BODY = """# Completion Report
**Status**: success

GATE GREEN — everything looks good. (No required sections, no evidence.)
"""

_NON_SUCCESS_BODY = """# Notes
Some free-form notes with no status field and none of the required sections.
"""


def _parse(tmp_path: Path, body: str) -> dict:
    p = tmp_path / "report.md"
    p.write_text(body, encoding="utf-8")
    return ReportParser().parse_report(str(p))


def test_valid_success_report_is_task_complete(tmp_path):
    r = _parse(tmp_path, _VALID_BODY)
    assert r["status"] == "success"
    assert r["event_type"] == "task_complete"
    assert r["contract_valid"] is True


def test_phantom_success_claim_is_contract_invalid(tmp_path):
    r = _parse(tmp_path, _PHANTOM_BODY)
    assert r["status"] == "contract_invalid"
    assert r["event_type"] == "report_contract_invalid"
    assert r["event"] == "report_contract_invalid"
    assert r["contract_valid"] is False


def test_non_success_report_is_not_reclassified(tmp_path):
    # No success claim -> keep task_complete even though the body is contract-invalid (it is not a
    # completion success-claim; gate/partial reports must not be reclassified).
    r = _parse(tmp_path, _NON_SUCCESS_BODY)
    assert r["status"] != "contract_invalid"
    assert r["event_type"] == "task_complete"
    assert r["contract_valid"] is False


# ---------------------------------------------------------------------------
# Non-report dispatch classes are exempted, not report_contract_invalid
# ---------------------------------------------------------------------------

_PANEL_SEAT_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: panel-architecture-diverge-1-abc123

GATE GREEN — deliberation seat verdict, no diff produced by design.
"""

_REVIEW_ROLE_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260716-plan-review-seat
**Role**: code-reviewer

GATE GREEN — review verdict only, no required sections.
"""

_REAL_BROKEN_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260716-real-broken-build
**Role**: backend-developer

GATE GREEN — everything looks good. (No required sections, no evidence.)
"""


def test_panel_seat_success_claim_is_exempt(tmp_path):
    r = _parse(tmp_path, _PANEL_SEAT_BODY)
    assert r["event_type"] == "report_exempt"
    assert r["status"] == "exempt"
    assert r["report_class"] == "panel_seat"


def test_review_role_success_claim_is_exempt(tmp_path):
    r = _parse(tmp_path, _REVIEW_ROLE_BODY)
    assert r["event_type"] == "report_exempt"
    assert r["report_class"] == "review_role"


def test_real_build_worker_broken_report_still_contract_invalid(tmp_path):
    """No exemption class applies: a genuinely broken build-worker report must
    still emit report_contract_invalid — over-exemption is a failure."""
    r = _parse(tmp_path, _REAL_BROKEN_BODY)
    assert r["event_type"] == "report_contract_invalid"
    assert r["status"] == "contract_invalid"
    assert "report_class" not in r


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
