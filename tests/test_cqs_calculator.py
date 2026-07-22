#!/usr/bin/env python3
"""Tests for CQS calculator — 7-component scoring with T0 Advisory and OI Delta."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure lib is importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPT_DIR))

from cqs_calculator import (
    _reconstruct_t0_risk_score,
    _score_completion,
    _score_t0_advisory,
    _score_open_items_delta,
    calculate_cqs,
    normalize_status,
)


# ── Completion gate shortcut (ADR-035 §9 PR-4b: verdict.decision, not quality_advisory) ──


def test_score_completion_gate_from_verdict_accept():
    receipt = {"verdict": {"decision": "accept"}}
    # gate signal only (report_path/pr_merged absent) → 100/3
    assert _score_completion(receipt) == pytest.approx(100.0 / 3)


def test_score_completion_gate_from_verdict_non_accept():
    receipt = {"verdict": {"decision": "investigate"}}
    assert _score_completion(receipt) == pytest.approx(0.0)


def test_score_completion_gate_legacy_fallback_when_verdict_absent():
    receipt = {"quality_advisory": {"t0_recommendation": {"decision": "approve"}}}
    assert _score_completion(receipt) == pytest.approx(100.0 / 3)


def test_score_completion_gate_verdict_priority_over_legacy():
    receipt = {
        "verdict": {"decision": "investigate"},
        "quality_advisory": {"t0_recommendation": {"decision": "approve"}},
    }
    assert _score_completion(receipt) == pytest.approx(0.0)


def test_score_completion_gate_passed_flag_still_wins():
    receipt = {"gate_passed": True}
    assert _score_completion(receipt) == pytest.approx(100.0 / 3)


# ── T0 Advisory scoring (ADR-035 §9 PR-4b: verdict{}/warnings[], not quality_advisory{}) ──


def test_score_t0_advisory_approve():
    receipt = {"verdict": {"decision": "accept"}, "warnings": []}
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(100.0)


def test_score_t0_advisory_hold():
    receipt = {
        "verdict": {"decision": "reject"},
        "warnings": [
            {"severity": "blocker"},
            {"severity": "blocker"},
        ],
    }
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(0.0)


def test_score_t0_advisory_followup_blended():
    receipt = {
        "verdict": {"decision": "investigate"},
        "warnings": [
            {"severity": "warn"},
            {"severity": "warn"},
            {"severity": "warn"},
            {"severity": "warn"},
        ],
    }
    # 60 * 0.7 + (100-40) * 0.3 = 42 + 18 = 60
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(60.0)


def test_score_t0_advisory_missing():
    score = _score_t0_advisory({})
    assert score == pytest.approx(50.0)


def test_score_t0_advisory_unavailable():
    receipt = {"verdict": {"status": "unavailable"}}
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(50.0)


def test_score_t0_advisory_legacy_fallback_when_verdict_absent():
    """A receipt written before PR-4 (or a DB-projection round-trip like
    update_dispatch_cqs.py replaying a historical quality_advisory_json
    column, OI-1175) has no verdict{} at all yet — the reader must still
    score it from quality_advisory{} until PR-5 removes that field."""
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {"decision": "hold"},
            "summary": {"risk_score": 100},
        }
    }
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(0.0)


def test_score_t0_advisory_verdict_takes_priority_over_legacy():
    """When a receipt carries both shapes (mixed-rollout window), verdict{}/
    warnings[] wins outright — never blended with quality_advisory{}."""
    receipt = {
        "verdict": {"decision": "accept"},
        "warnings": [],
        "quality_advisory": {
            "t0_recommendation": {"decision": "hold"},
            "summary": {"risk_score": 100},
        },
    }
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(100.0)


# ── T28: CQS-score parity — quality_advisory{}-shaped input vs the equivalent
#    warnings[]/verdict{}-shaped input must yield the SAME T0-Advisory score
#    (ADR-035 §3.3 HIGH-5, the regression guard required before PR-5 drops
#    quality_advisory{}). `_legacy_score_t0_advisory` below is a frozen copy
#    of the pre-migration formula (the exact code _score_t0_advisory carried
#    before this PR) — the oracle this test proves the migrated reader still
#    agrees with, for equivalent inputs expressed in each shape.


def _legacy_score_t0_advisory(receipt):
    advisory = receipt.get("quality_advisory")
    if not isinstance(advisory, dict):
        return 50.0

    rec = advisory.get("t0_recommendation")
    if not isinstance(rec, dict):
        return 50.0

    decision = rec.get("decision", "approve")
    risk_score = advisory.get("summary", {}).get("risk_score", 0)

    decision_scores = {"approve": 100.0, "approve_with_followup": 60.0, "hold": 0.0}
    decision_score = decision_scores.get(decision, 50.0)

    return decision_score * 0.7 + max(0, 100 - risk_score) * 0.3


@pytest.mark.parametrize(
    "legacy_decision,legacy_risk_score,new_decision,warning_severities",
    [
        ("approve", 0, "accept", []),
        ("approve", 20, "accept", ["warn", "warn"]),
        ("approve_with_followup", 40, "investigate", ["warn"] * 4),
        ("approve_with_followup", 50, "investigate", ["blocker"]),
        ("hold", 100, "reject", ["blocker", "blocker"]),
        ("hold", 60, "reject", ["blocker", "warn"]),
    ],
)
def test_t28_cqs_score_parity_quality_advisory_vs_verdict_warnings(
    legacy_decision, legacy_risk_score, new_decision, warning_severities
):
    legacy_receipt = {
        "quality_advisory": {
            "t0_recommendation": {"decision": legacy_decision},
            "summary": {"risk_score": legacy_risk_score},
        }
    }
    new_receipt = {
        "verdict": {"decision": new_decision},
        "warnings": [{"severity": s} for s in warning_severities],
    }

    legacy_score = _legacy_score_t0_advisory(legacy_receipt)
    new_score = _score_t0_advisory(new_receipt)

    assert new_score == pytest.approx(legacy_score)


def test_t28_risk_score_reconstruction_matches_legacy_weights():
    # RISK_WEIGHT_BLOCKING=50, RISK_WEIGHT_WARNING=10 (quality_advisory.py) —
    # 1 blocker + 2 warn = 50 + 20 = 70, same as the legacy dry-run's weighted sum.
    warnings = [{"severity": "blocker"}, {"severity": "warn"}, {"severity": "warn"}]
    assert _reconstruct_t0_risk_score(warnings) == pytest.approx(70)


def test_t28_risk_score_reconstruction_caps_at_100():
    warnings = [{"severity": "blocker"}] * 5  # 250 raw, capped
    assert _reconstruct_t0_risk_score(warnings) == pytest.approx(100)


# ── Open Items Delta scoring ──


def test_score_oi_delta_resolved_bonus():
    receipt = {"open_items_resolved": 2, "open_items_created": 0}
    score = _score_open_items_delta(receipt)
    # 50 + min(30, 2*15) = 50 + 30 = 80
    assert score == pytest.approx(80.0)


def test_score_oi_delta_created_penalty():
    receipt = {"open_items_created": 3, "open_items_resolved": 0}
    score = _score_open_items_delta(receipt)
    # 50 - min(30, 3*10) = 50 - 30 = 20
    assert score == pytest.approx(20.0)


def test_score_oi_delta_targeted_unresolved():
    receipt = {
        "target_open_items": ["OI-042", "OI-043"],
        "open_items_resolved": 0,
        "open_items_created": 0,
    }
    score = _score_open_items_delta(receipt)
    # 50 - min(20, 2*20) = 50 - 20 = 30 (unresolved targets)
    # Note: targeted=2, resolved=0 → (2-0)*20 = 40 capped at 20
    assert score == pytest.approx(30.0)


def test_score_oi_delta_neutral():
    score = _score_open_items_delta({})
    assert score == pytest.approx(50.0)


# ── Weights validation ──


def test_weights_sum_to_1():
    receipt = {"status": "task_complete"}
    result = calculate_cqs(receipt, None)
    weights = result["components"]["weights"]
    assert sum(weights.values()) == pytest.approx(1.0)
    assert len(weights) == 7


# ── Timeout exclusion ──


def test_backward_compat_timeout_excluded():
    receipt = {"status": "timeout"}
    result = calculate_cqs(receipt, None)
    assert result["cqs"] is None
    assert result["normalized_status"] == "timeout"


# ── Full 7-component CQS ──


@patch("cqs_calculator._get_role_median_tokens", return_value=None)
def test_full_cqs_7_components(mock_median):
    receipt = {
        "status": "task_complete",
        "report_path": "/some/report.md",
        "verdict": {"decision": "accept"},
        "warnings": [{"severity": "warn"}],
        "open_items_created": 0,
        "open_items_resolved": 1,
    }
    result = calculate_cqs(receipt, None)
    assert result["cqs"] is not None
    assert 0 <= result["cqs"] <= 100
    assert result["normalized_status"] == "success"

    components = result["components"]
    assert "t0_advisory" in components
    assert "oi_delta" in components
    assert components["t0_advisory"] > 50  # approve with low risk
    assert components["oi_delta"] > 50  # resolved 1, created 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
