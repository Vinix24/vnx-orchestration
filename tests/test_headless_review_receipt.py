#!/usr/bin/env python3
"""Tests for the headless review receipt schema, validation, and normalization."""

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from headless_review_receipt import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    VALID_STATUSES,
    HeadlessReviewReceipt,
    ValidationError,
    normalize_gate_result,
    validate_gate_result,
    validate_report_path_exists,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_valid_result(**overrides):
    """Return a minimal valid gate result dict."""
    base = {
        "gate": "gemini_review",
        "pr_id": "PR-1",
        "branch": "feature/test",
        "status": "pass",
        "summary": "all checks passed",
        "contract_hash": "abcd1234efgh5678",
        "report_path": "/tmp/reports/headless/20260331-gemini-PR-1.md",
        "blocking_findings": [],
        "advisory_findings": [],
        "blocking_count": 0,
        "advisory_count": 0,
        "required_reruns": [],
        "residual_risk": "",
        "recorded_at": "2026-03-31T14:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# HeadlessReviewReceipt dataclass
# ---------------------------------------------------------------------------

class TestHeadlessReviewReceipt:
    def test_from_dict_roundtrip(self):
        d = _make_valid_result()
        receipt = HeadlessReviewReceipt.from_dict(d)
        assert receipt.gate == "gemini_review"
        assert receipt.pr_id == "PR-1"
        assert receipt.status == "pass"
        assert receipt.report_path == d["report_path"]
        rt = receipt.to_dict()
        for field in REQUIRED_FIELDS:
            assert field in rt

    def test_json_roundtrip(self):
        d = _make_valid_result()
        receipt = HeadlessReviewReceipt.from_dict(d)
        text = receipt.to_json()
        restored = HeadlessReviewReceipt.from_json(text)
        assert restored.gate == receipt.gate
        assert restored.contract_hash == receipt.contract_hash

    def test_is_pass_true(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result())
        assert receipt.is_pass() is True

    def test_is_pass_false_when_blocking(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result(
            blocking_findings=[{"severity": "blocking", "message": "bug"}],
            blocking_count=1,
        ))
        assert receipt.is_pass() is False

    def test_is_pass_false_when_failed(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result(status="fail"))
        assert receipt.is_pass() is False

    def test_is_contradictory(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result(
            status="pass",
            blocking_findings=[{"severity": "blocking", "message": "oops"}],
            blocking_count=1,
        ))
        assert receipt.is_contradictory() is True

    def test_is_not_contradictory(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result())
        assert receipt.is_contradictory() is False

    def test_to_dict_reconciles_counts(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result(
            blocking_findings=[{"severity": "blocking", "message": "x"}],
            blocking_count=999,
            advisory_findings=[{"severity": "advisory", "message": "y"}],
            advisory_count=999,
        ))
        d = receipt.to_dict()
        assert d["blocking_count"] == 1
        assert d["advisory_count"] == 1

    def test_codex_verdict_used_for_is_pass(self):
        receipt = HeadlessReviewReceipt.from_dict(_make_valid_result(
            status="",
            verdict="pass",
        ))
        assert receipt.is_pass() is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateGateResult:
    def test_valid_result_no_errors(self):
        errors = validate_gate_result(_make_valid_result())
        assert len([e for e in errors if e.severity == "error"]) == 0

    def test_missing_required_field(self):
        d = _make_valid_result()
        del d["report_path"]
        errors = validate_gate_result(d)
        field_names = [e.field for e in errors]
        assert "report_path" in field_names

    def test_empty_identity_field(self):
        d = _make_valid_result(pr_id="")
        errors = validate_gate_result(d)
        assert any(e.field == "pr_id" and e.severity == "error" for e in errors)

    def test_unrecognized_status_warning(self):
        d = _make_valid_result(status="banana")
        errors = validate_gate_result(d)
        assert any(e.field == "status" and e.severity == "warning" for e in errors)

    def test_missing_report_path_for_pass(self):
        d = _make_valid_result(report_path="")
        errors = validate_gate_result(d)
        assert any(e.field == "report_path" and e.severity == "error" for e in errors)

    def test_report_path_not_required_for_blocked(self):
        d = _make_valid_result(status="blocked", report_path="")
        errors = validate_gate_result(d)
        report_errors = [e for e in errors if e.field == "report_path" and e.severity == "error"]
        assert len(report_errors) == 0

    def test_contradictory_pass_with_blockers(self):
        d = _make_valid_result(
            status="pass",
            blocking_findings=[{"severity": "blocking", "message": "fail"}],
            blocking_count=1,
        )
        errors = validate_gate_result(d)
        assert any("contradictory" in e.message for e in errors)

    def test_count_mismatch_warning(self):
        d = _make_valid_result(blocking_count=5)
        errors = validate_gate_result(d)
        assert any(e.field == "blocking_count" and e.severity == "warning" for e in errors)

    def test_missing_contract_hash_for_pass(self):
        d = _make_valid_result(contract_hash="")
        errors = validate_gate_result(d)
        assert any(e.field == "contract_hash" for e in errors)

    def test_findings_not_list(self):
        d = _make_valid_result(blocking_findings="not a list")
        errors = validate_gate_result(d)
        assert any(e.field == "blocking_findings" for e in errors)


class TestValidateReportPathExists:
    def test_existing_file(self, tmp_path):
        report = tmp_path / "report.md"
        report.write_text("# Report\n")
        assert validate_report_path_exists(str(report)) is None

    def test_missing_file(self):
        err = validate_report_path_exists("/nonexistent/path/report.md")
        assert err is not None
        assert err.field == "report_path"

    def test_empty_path(self):
        err = validate_report_path_exists("")
        assert err is not None
        assert "empty" in err.message

    def test_directory_not_file(self, tmp_path):
        err = validate_report_path_exists(str(tmp_path))
        assert err is not None
        assert "not a file" in err.message


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalizeGateResult:
    def test_fills_missing_fields(self):
        partial = {"gate": "gemini_review", "pr_id": "PR-1", "status": "pass"}
        normalized = normalize_gate_result(partial)
        for f in REQUIRED_FIELDS:
            assert f in normalized

    def test_preserves_existing_values(self):
        d = _make_valid_result()
        normalized = normalize_gate_result(d)
        assert normalized["gate"] == "gemini_review"
        assert normalized["pr_id"] == "PR-1"

    def test_sets_report_path_from_kwarg(self):
        partial = {"gate": "codex_gate", "pr_id": "PR-2", "status": "pass"}
        normalized = normalize_gate_result(partial, report_path="/reports/codex.md")
        assert normalized["report_path"] == "/reports/codex.md"

    def test_does_not_override_existing_report_path(self):
        d = _make_valid_result(report_path="/existing/path.md")
        normalized = normalize_gate_result(d, report_path="/other/path.md")
        assert normalized["report_path"] == "/existing/path.md"

    def test_reconciles_counts(self):
        d = {
            "gate": "gemini_review",
            "pr_id": "PR-1",
            "status": "fail",
            "blocking_findings": [{"severity": "blocking", "message": "x"}],
            "advisory_findings": [],
        }
        normalized = normalize_gate_result(d)
        assert normalized["blocking_count"] == 1
        assert normalized["advisory_count"] == 0


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

class TestSchemaConstants:
    def test_all_valid_statuses_covered(self):
        expected = {"pass", "fail", "blocked", "pending", "not_configured", "configured_dry_run"}
        assert VALID_STATUSES == expected

    def test_required_fields_match_contract_section_4(self):
        contract_fields = {
            "gate", "pr_id", "branch", "status", "summary",
            "contract_hash", "report_path",
            "blocking_findings", "advisory_findings",
            "blocking_count", "advisory_count",
            "required_reruns", "residual_risk", "recorded_at",
        }
        assert REQUIRED_FIELDS == contract_fields
