#!/usr/bin/env python3

import sys
from pathlib import Path


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from auto_merge_policy import codex_final_gate_required, evaluate_auto_merge_policy


def test_conditional_auto_merge_allowed_for_low_risk_docs_change():
    decision = evaluate_auto_merge_policy(
        risk_class="low",
        merge_policy="conditional_auto",
        changed_files=["docs/quickstart.md", "README.md"],
        gemini_review_passed=True,
        codex_gate_passed=True,
        required_checks_passed=True,
        closure_verifier_passed=True,
    )

    assert decision.allowed is True
    assert decision.blockers == []


def test_conditional_auto_merge_blocked_for_high_risk_runtime_scope():
    decision = evaluate_auto_merge_policy(
        risk_class="low",
        merge_policy="conditional_auto",
        changed_files=["scripts/dispatcher_v8_minimal.sh"],
        gemini_review_passed=True,
        codex_gate_passed=True,
        required_checks_passed=True,
        closure_verifier_passed=True,
    )

    assert decision.allowed is False
    assert "high_risk_change_scope" in decision.blockers
    assert codex_final_gate_required(["scripts/dispatcher_v8_minimal.sh"]) is True


def test_conditional_auto_merge_blocked_when_risk_not_low():
    decision = evaluate_auto_merge_policy(
        risk_class="medium",
        merge_policy="conditional_auto",
        changed_files=["docs/ops.md"],
        gemini_review_passed=True,
        codex_gate_passed=True,
        required_checks_passed=True,
        closure_verifier_passed=True,
    )

    assert decision.allowed is False
    assert "risk_class_not_low" in decision.blockers
