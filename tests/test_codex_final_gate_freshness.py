#!/usr/bin/env python3
"""OI-1228 regression: empty content_hash must fail closed (treated as stale).

Background: ``check_gate_clearance`` previously skipped the stale-receipt check
entirely whenever either side of the hash comparison was empty. A receipt with
no ``content_hash`` therefore cleared the gate even though it carried no proof
of being for the current contract revision.
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


def _required_contract(content_hash: str = "hash-current") -> ReviewContract:
    return ReviewContract(
        pr_id="PR-OI1228",
        pr_title="freshness regression",
        risk_class="high",
        review_stack=["codex_gate"],
        changed_files=["scripts/dispatcher.py"],
        content_hash=content_hash,
    )


def _passing_receipt(content_hash: str) -> CodexFinalGateReceipt:
    return CodexFinalGateReceipt(
        pr_id="PR-OI1228",
        verdict="pass",
        required=True,
        findings=[],
        content_hash=content_hash,
    )


def test_empty_receipt_hash_fails_closed():
    """Receipt with empty content_hash must trip the stale-receipt blocker."""
    contract = _required_contract()
    receipt = _passing_receipt(content_hash="")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert "codex_gate_stale_receipt" in result["blockers"]


def test_empty_contract_hash_fails_closed():
    """Contract with empty content_hash must trip the stale-receipt blocker."""
    contract = _required_contract(content_hash="")
    receipt = _passing_receipt(content_hash="hash-something")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert "codex_gate_stale_receipt" in result["blockers"]


def test_both_hashes_empty_fails_closed():
    contract = _required_contract(content_hash="")
    receipt = _passing_receipt(content_hash="")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert "codex_gate_stale_receipt" in result["blockers"]


def test_matching_hashes_clears_gate():
    """Sanity check: matching, non-empty hashes still clear the gate."""
    contract = _required_contract(content_hash="hash-current")
    receipt = _passing_receipt(content_hash="hash-current")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is True
    assert result["blockers"] == []


def test_mismatched_hashes_still_block():
    """Sanity check: existing stale-receipt blocker still fires on mismatch."""
    contract = _required_contract(content_hash="hash-current")
    receipt = _passing_receipt(content_hash="hash-old")

    result = check_gate_clearance(contract, receipt)

    assert result["cleared"] is False
    assert "codex_gate_stale_receipt" in result["blockers"]
