#!/usr/bin/env python3
"""Tests: check_gate_clearance must propagate deletion warnings even when gate clears."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_final_gate import (
    DELETION_FILE_WARN,
    NET_LINE_DELETION_WARN,
    CodexFinalGateReceipt,
    check_gate_clearance,
)
from review_contract import Deliverable, ReviewContract


def _make_contract(**overrides) -> ReviewContract:
    defaults = dict(
        pr_id="PR-warn-test",
        pr_title="Clearance warning test",
        feature_title="test",
        branch="feat/test",
        track="A",
        risk_class="low",
        merge_policy="squash_merge",
        closure_stage="open",
        deliverables=[Deliverable(description="cleanup", category="infrastructure")],
        review_stack=[],
        changed_files=[],
        content_hash="abc123",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


def _mock_deleted(files: list):
    m = MagicMock()
    m.returncode = 0
    m.stdout = "\n".join(files) + "\n" if files else ""
    return m


def _mock_numstat(net_removed: int):
    m = MagicMock()
    m.returncode = 0
    m.stdout = f"0\t{net_removed}\tsome/file.py\n"
    return m


class TestClearanceWarnings:
    """check_gate_clearance must always include a 'warnings' key."""

    def test_cleared_no_warnings_has_key(self, tmp_path):
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted([]),
            _mock_numstat(0),
        ]):
            result = check_gate_clearance(contract, receipt=None, project_root=tmp_path)
        assert "warnings" in result
        assert result["cleared"] is True
        assert result["warnings"] == []

    def test_gate_not_required_with_warn_level_deletion_surfaces_warning(self, tmp_path):
        """When gate is not required but WARN-level file deletion exists, warnings is non-empty."""
        contract = _make_contract()
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_WARN)]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted(deleted),
            _mock_numstat(0),
        ]):
            result = check_gate_clearance(contract, receipt=None, project_root=tmp_path)
        assert result["cleared"] is True
        assert result["reason"] == "codex_gate_not_required"
        assert "mass_deletion_warning" in result["warnings"]

    def test_gate_not_required_with_net_line_warn_surfaces_warning(self, tmp_path):
        """WARN-level net line deletion shows up in warnings even when gate is not required."""
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted([]),
            _mock_numstat(NET_LINE_DELETION_WARN + 50),
        ]):
            result = check_gate_clearance(contract, receipt=None, project_root=tmp_path)
        assert result["cleared"] is True
        assert "net_line_deletion_warning" in result["warnings"]

    def test_gate_required_no_receipt_includes_warnings(self, tmp_path):
        """When gate is required and receipt is missing, warnings still propagate."""
        contract = _make_contract()
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_WARN)]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted(deleted),
            _mock_numstat(0),
        ]):
            # WARN-level only: gate not required but set risk_class high to force requirement
            contract_high = _make_contract(risk_class="high")
            result = check_gate_clearance(contract_high, receipt=None, project_root=tmp_path)
        assert result["cleared"] is False
        assert "warnings" in result

    def test_cleared_with_passing_receipt_includes_warnings(self, tmp_path):
        """Even a passing receipt clearance includes deletion warnings."""
        contract = _make_contract(content_hash="deadbeef")
        receipt = CodexFinalGateReceipt(
            pr_id="PR-warn-test",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="deadbeef",
            prompt_rendered=True,
            recorded_at="2026-05-17T00:00:00Z",
        )
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_WARN)]
        contract_high = _make_contract(risk_class="high", content_hash="deadbeef")
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted(deleted),
            _mock_numstat(0),
        ]):
            result = check_gate_clearance(contract_high, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is True
        assert "warnings" in result
        assert result["warnings"] == ["mass_deletion_warning"]

    def test_cleared_clean_pr_empty_warnings(self, tmp_path):
        """Clean PR: warnings is empty list, not absent."""
        contract = _make_contract(risk_class="high", content_hash="abc")
        receipt = CodexFinalGateReceipt(
            pr_id="PR-warn-test",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="abc",
            prompt_rendered=True,
            recorded_at="2026-05-17T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_deleted([]),
            _mock_numstat(0),
        ]):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is True
        assert result["warnings"] == []
