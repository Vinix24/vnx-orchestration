#!/usr/bin/env python3
"""Tests for the report_parser body-contract enforcement (audit governance #10).

Dispatch-ID: 20260627-audit-report-parser-contract

The receipt processor must not admit a success-claim with an invalid report body into the audit
trail as a clean task_complete. A success-claim + invalid body -> report_contract_invalid; a
contract-valid success report -> task_complete; a non-success report is never reclassified.
"""

import json
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


def test_review_role_body_without_spec_no_longer_exempt(tmp_path):
    """fix-r2 Finding 1 BLOCKING (T-adv5): a report-body role, with no
    authoritative dispatch-spec.json backing it (no state_dir passed here,
    so no spec can ever be found), can no longer grant an exemption.
    Previously this returned report_exempt/review_role."""
    r = _parse(tmp_path, _REVIEW_ROLE_BODY)
    assert r["event_type"] == "report_contract_invalid"
    assert r["status"] == "contract_invalid"
    assert "report_class" not in r


def test_real_build_worker_broken_report_still_contract_invalid(tmp_path):
    """No exemption class applies: a genuinely broken build-worker report must
    still emit report_contract_invalid — over-exemption is a failure."""
    r = _parse(tmp_path, _REAL_BROKEN_BODY)
    assert r["event_type"] == "report_contract_invalid"
    assert r["status"] == "contract_invalid"
    assert "report_class" not in r


# ---------------------------------------------------------------------------
# codex-gate fix-round (#1184) — Finding 1 BLOCKING: classify off the
# AUTHORITATIVE dispatch-spec.json, never the worker's own report body.
# report_parser.py has its own copy of the classification call site
# (_build_enhanced_receipt) — same vulnerability, same fix, own coverage.
# ---------------------------------------------------------------------------

def _write_dispatch_spec(data_dir: Path, dispatch_id: str, role: str, status: str = "pending") -> None:
    spec_dir = data_dir / "dispatches" / status / dispatch_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "dispatch-spec.json").write_text(
        json.dumps({
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": dispatch_id,
            "staging_id": dispatch_id,
            "instruction_file": str(spec_dir / "instruction.md"),
            "role": role,
            "target_slot": "T1",
        }),
        encoding="utf-8",
    )


def test_forged_self_exempt_fields_do_not_bypass_contract_with_spec(tmp_path):
    """T-adv1 (report_parser.py call site): a spec-backed backend-developer
    dispatch forges read_only/role/task_class exemption fields in its own
    report body. Must still get report_contract_invalid, NOT report_exempt."""
    did = "20260716-t-adv1-parser-self-exempt"
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    _write_dispatch_spec(data_dir, did, "backend-developer")

    body = f"""# Completion Report
**Status**: success
**Dispatch-ID**: {did}
**Role**: code-reviewer
**Task-Class**: research_structured
**Read-Only**: true

GATE GREEN — everything looks good. (No required sections, no evidence.)
"""
    p = tmp_path / "report.md"
    p.write_text(body, encoding="utf-8")

    r = ReportParser(state_dir=state_dir).parse_report(str(p))
    assert r["event_type"] == "report_contract_invalid"
    assert r["status"] == "contract_invalid"
    assert "report_class" not in r


def test_panel_seat_without_spec_still_exempt(tmp_path):
    """T-adv2 (report_parser.py call site): a genuinely ungoverned panel
    seat (no dispatch-spec.json anywhere) still gets report_exempt."""
    did = "panel-t-adv2-parser-arch-diverge"
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)

    body = f"""# Completion Report
**Status**: success
**Dispatch-ID**: {did}

GATE GREEN — deliberation seat verdict, no diff produced by design.
"""
    p = tmp_path / "report.md"
    p.write_text(body, encoding="utf-8")

    r = ReportParser(state_dir=state_dir).parse_report(str(p))
    assert r["event_type"] == "report_exempt"
    assert r["status"] == "exempt"
    assert r["report_class"] == "panel_seat"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
