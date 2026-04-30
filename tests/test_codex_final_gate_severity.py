#!/usr/bin/env python3
"""OI-1227 regression: codex_final_gate severity comparison must be case-insensitive.

Background: ``check_gate_clearance`` previously did ``severity == "error"`` literal
comparison, so a finding with ``"severity": "Error"`` (or ``"ERROR"``, ``"BLOCKER"``)
silently bypassed the error-finding blocker and let a failing gate clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

from codex_final_gate import (  # noqa: E402
    CodexFinalGateReceipt,
    check_gate_clearance,
)
from review_contract import ReviewContract  # noqa: E402


def _required_contract() -> ReviewContract:
    """Contract that always requires a Codex final gate."""
    return ReviewContract(
        pr_id="PR-OI1227",
        pr_title="severity case-insensitivity regression",
        risk_class="high",
        review_stack=["codex_gate"],
        changed_files=["scripts/dispatcher.py"],
        content_hash="hash-1",
    )


def _receipt_with_severity(severity: str) -> CodexFinalGateReceipt:
    return CodexFinalGateReceipt(
        pr_id="PR-OI1227",
        verdict="pass",
        required=True,
        findings=[{"severity": severity, "message": "broke something"}],
        content_hash="hash-1",
    )


def test_severity_capitalised_error_still_blocks():
    """Severity ``Error`` (mixed case) must still count as an error finding."""
    contract = _required_contract()
    receipt = _receipt_with_severity("Error")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert any("unresolved_errors" in b for b in result["blockers"])


def test_severity_uppercase_error_still_blocks():
    contract = _required_contract()
    receipt = _receipt_with_severity("ERROR")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert any("unresolved_errors" in b for b in result["blockers"])


def test_severity_blocker_counts_as_error():
    """Severity ``blocker`` is at least as severe as error and must block too."""
    contract = _required_contract()
    receipt = _receipt_with_severity("blocker")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert any("unresolved_errors" in b for b in result["blockers"])


def test_severity_warning_does_not_block():
    """Sanity check: warnings still allow the gate to clear."""
    contract = _required_contract()
    receipt = _receipt_with_severity("warning")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is True
    assert result["blockers"] == []
