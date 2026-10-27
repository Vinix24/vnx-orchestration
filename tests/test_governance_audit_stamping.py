#!/usr/bin/env python3
"""Tests for OI-AT-5: dispatch_id + pr_number stamping on governance_audit rows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import governance_audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)


def _read_entries(tmp_path: Path) -> list[dict]:
    audit_file = tmp_path / "state" / "governance_audit.ndjson"
    if not audit_file.exists():
        return []
    return [json.loads(ln) for ln in audit_file.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# test_writer_stamps_dispatch_id_when_provided
# ---------------------------------------------------------------------------

def test_writer_stamps_dispatch_id_when_provided(tmp_path, monkeypatch):
    """log_enforcement() stamps dispatch_id when explicitly provided."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_enforcement(
        check_name="receipt_must_have_commit",
        level=2,
        result=True,
        context={"feature": "OI-AT-5"},
        message="Commit found",
        dispatch_id="20260424-000300-governance-audit-stamping-A",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["dispatch_id"] == "20260424-000300-governance-audit-stamping-A"


# ---------------------------------------------------------------------------
# test_writer_stamps_pr_number_when_provided
# ---------------------------------------------------------------------------

def test_writer_stamps_pr_number_when_provided(tmp_path, monkeypatch):
    """log_enforcement() stamps pr_number when provided in context."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_enforcement(
        check_name="gate_before_next_feature",
        level=2,
        result=True,
        context={"pr_number": 261, "feature": "OI-AT-5"},
        message="Gate passed",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["pr_number"] == 261


def test_gate_result_stamps_dispatch_id_when_provided(tmp_path, monkeypatch):
    """log_gate_result() stamps dispatch_id when explicitly provided."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_gate_result(
        gate="codex_gate",
        pr_number=261,
        status="triggered",
        findings_count=0,
        dispatch_id="20260424-000300-governance-audit-stamping-A",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["dispatch_id"] == "20260424-000300-governance-audit-stamping-A"
    assert e["pr_number"] == 261
    assert e["event_type"] == "gate_result"


def test_dispatch_decision_stamps_pr_number_when_provided(tmp_path, monkeypatch):
    """log_dispatch_decision() stamps pr_number when explicitly provided."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_dispatch_decision(
        action="blocked",
        dispatch_id="20260424-000300-governance-audit-stamping-A",
        reasoning="Governance checks failed",
        pr_number=261,
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["dispatch_id"] == "20260424-000300-governance-audit-stamping-A"
    assert e["pr_number"] == 261
    assert e["event_type"] == "dispatch_decision"


# ---------------------------------------------------------------------------
# test_writer_null_when_caller_has_no_context (backward compat)
# ---------------------------------------------------------------------------

def test_writer_null_when_caller_has_no_context(tmp_path, monkeypatch):
    """dispatch_id and pr_number are null when caller provides no context."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_enforcement(
        check_name="ci_green_required",
        level=3,
        result=False,
        context={},
        message="CI not green",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["dispatch_id"] is None
    assert e["pr_number"] is None


def test_gate_result_null_dispatch_id_when_absent(tmp_path, monkeypatch):
    """log_gate_result() dispatch_id is null when not passed."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_gate_result(
        gate="gemini_review",
        pr_number=None,
        status="failed",
        findings_count=2,
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["dispatch_id"] is None


def test_dispatch_decision_null_pr_number_when_absent(tmp_path, monkeypatch):
    """log_dispatch_decision() pr_number is null when not passed."""
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_dispatch_decision(
        action="accepted",
        dispatch_id="some-dispatch-id",
        reasoning="All checks passed",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["pr_number"] is None
    assert entries[0]["dispatch_id"] == "some-dispatch-id"


# ---------------------------------------------------------------------------
# test_caller_passes_receipt_dispatch_id (integration: cleanup_orphan_gates)
# ---------------------------------------------------------------------------

def test_caller_passes_receipt_dispatch_id(tmp_path, monkeypatch):
    """cleanup_orphan_gates._log_to_audit() stamps pr_number from stem and dispatch_id from request_data."""
    _set_data_dir(tmp_path, monkeypatch)

    import cleanup_orphan_gates

    orphans = [
        {
            "stem": "pr-57-gemini_review",
            "age_hours": 26.3,
            "gate_name": "gemini_review",
            "request_data": {"dispatch_id": "f51-pr3-t3-dispatch"},
        }
    ]

    cleanup_orphan_gates._log_to_audit(orphans)

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["pr_number"] == 57
    assert e["dispatch_id"] == "f51-pr3-t3-dispatch"
    assert e["check_name"] == "orphan_gate_cleanup"


def test_cleanup_orphan_gates_no_pr_in_stem(tmp_path, monkeypatch):
    """Orphan with non-standard stem has pr_number=None (no crash)."""
    _set_data_dir(tmp_path, monkeypatch)

    import cleanup_orphan_gates

    orphans = [
        {
            "stem": "legacy-gate-name",
            "age_hours": 30.0,
            "gate_name": "legacy",
            "request_data": {},
        }
    ]

    cleanup_orphan_gates._log_to_audit(orphans)

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["pr_number"] is None
    assert e["dispatch_id"] is None
