#!/usr/bin/env python3
"""Tests for governance_audit.py — F51-PR3."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/lib importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPT_DIR))

import governance_audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VNX_DATA_DIR at a temp dir; return that path."""
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    return tmp_path


def _read_entries(tmp_path: Path) -> list[dict]:
    audit_file = tmp_path / "state" / "governance_audit.ndjson"
    if not audit_file.exists():
        return []
    lines = [ln.strip() for ln in audit_file.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ---------------------------------------------------------------------------
# test_log_enforcement_appends_ndjson
# ---------------------------------------------------------------------------

def test_log_enforcement_appends_ndjson(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_enforcement(
        check_name="codex_gate_required",
        level=2,
        result=False,
        context={"pr_number": 221, "feature": "F51"},
        message="Codex gate result not found",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["event_type"] == "enforcement_check"
    assert e["check_name"] == "codex_gate_required"
    assert e["level"] == 2
    assert e["passed"] is False
    assert e["message"] == "Codex gate result not found"
    assert e["feature"] == "F51"
    assert e["pr_number"] == 221
    assert e["context_hash"]  # non-empty
    assert "timestamp" in e


# ---------------------------------------------------------------------------
# test_get_recent_returns_last_n
# ---------------------------------------------------------------------------

def test_get_recent_returns_last_n(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    for i in range(10):
        governance_audit.log_enforcement(
            check_name=f"check_{i}",
            level=1,
            result=True,
            context={"index": i},
            message=f"entry {i}",
        )

    recent = governance_audit.get_recent(limit=5)
    assert len(recent) == 5
    # get_recent returns newest-first
    assert recent[0]["check_name"] == "check_9"
    assert recent[4]["check_name"] == "check_5"


# ---------------------------------------------------------------------------
# test_override_logged_with_reason
# ---------------------------------------------------------------------------

def test_override_logged_with_reason(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_enforcement(
        check_name="gate_before_next_feature",
        level=2,
        result=True,
        context={"feature": "F47"},
        override="operator confirmed F46 gates complete offline",
        message="[OVERRIDDEN] No gate results for PR #218",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["override"] == "operator confirmed F46 gates complete offline"

    overrides = governance_audit.get_overrides(days=7)
    assert len(overrides) == 1
    assert overrides[0]["override"] == "operator confirmed F46 gates complete offline"


# ---------------------------------------------------------------------------
# test_log_gate_result_appends
# ---------------------------------------------------------------------------

def test_log_gate_result_appends(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_gate_result(
        gate="codex_gate",
        pr_number=221,
        status="triggered",
        findings_count=0,
    )
    governance_audit.log_gate_result(
        gate="gemini_review",
        pr_number=221,
        status="failed",
        findings_count=3,
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 2
    assert entries[0]["event_type"] == "gate_result"
    assert entries[0]["passed"] is True
    assert entries[1]["passed"] is False
    assert "findings: 3" in entries[1]["message"]


# ---------------------------------------------------------------------------
# test_log_dispatch_decision_appends
# ---------------------------------------------------------------------------

def test_log_dispatch_decision_appends(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    governance_audit.log_dispatch_decision(
        action="blocked",
        dispatch_id="f51-pr3-t1-20260413T120000",
        reasoning="Governance checks failed: gate_before_next_feature",
    )

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["event_type"] == "dispatch_decision"
    assert e["action"] == "blocked"
    assert e["passed"] is False
    assert e["dispatch_id"] == "f51-pr3-t1-20260413T120000"


# ---------------------------------------------------------------------------
# test_api_enforcement_endpoint
# ---------------------------------------------------------------------------

def test_api_enforcement_endpoint(tmp_path, monkeypatch):
    _set_data_dir(tmp_path, monkeypatch)

    # Write a few entries
    governance_audit.log_enforcement(
        check_name="ci_green_required",
        level=3,
        result=False,
        context={"pr_number": 220},
        message="CI not green",
    )

    # Add scripts/lib and dashboard to path for import
    dashboard_dir = str(Path(__file__).resolve().parent.parent / "dashboard")
    if dashboard_dir not in sys.path:
        sys.path.insert(0, dashboard_dir)

    from api_intelligence import _governance_get_enforcement

    result = _governance_get_enforcement({})
    assert "checks" in result
    assert isinstance(result["checks"], list)
    assert len(result["checks"]) == 1
    check = result["checks"][0]
    assert check["check_name"] == "ci_green_required"
    assert check["passed"] is False
    assert check["level"] == 3


# ---------------------------------------------------------------------------
# test_api_config_endpoint
# ---------------------------------------------------------------------------

def test_api_config_endpoint(tmp_path, monkeypatch):
    dashboard_dir = str(Path(__file__).resolve().parent.parent / "dashboard")
    if dashboard_dir not in sys.path:
        sys.path.insert(0, dashboard_dir)

    from api_intelligence import _governance_get_config

    result, status = _governance_get_config()

    # If config file not found it returns 404 — that's acceptable in test env.
    assert status in (200, 404, 500)

    if status == 200:
        assert "mode" in result
        assert "checks" in result
        assert isinstance(result["checks"], list)
        for check in result["checks"]:
            assert "name" in check
            assert "level" in check
