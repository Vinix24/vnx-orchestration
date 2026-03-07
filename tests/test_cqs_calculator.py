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
    _score_t0_advisory,
    _score_open_items_delta,
    calculate_cqs,
    normalize_status,
)


# ── T0 Advisory scoring ──


def test_score_t0_advisory_approve():
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {"decision": "approve"},
            "summary": {"risk_score": 0},
        }
    }
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(100.0)


def test_score_t0_advisory_hold():
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {"decision": "hold"},
            "summary": {"risk_score": 100},
        }
    }
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(0.0)


def test_score_t0_advisory_followup_blended():
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {"decision": "approve_with_followup"},
            "summary": {"risk_score": 40},
        }
    }
    # 60 * 0.7 + (100-40) * 0.3 = 42 + 18 = 60
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(60.0)


def test_score_t0_advisory_missing():
    score = _score_t0_advisory({})
    assert score == pytest.approx(50.0)


def test_score_t0_advisory_unavailable():
    receipt = {"quality_advisory": {"status": "unavailable"}}
    score = _score_t0_advisory(receipt)
    assert score == pytest.approx(50.0)


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
        "quality_advisory": {
            "t0_recommendation": {"decision": "approve"},
            "summary": {"risk_score": 10},
        },
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
