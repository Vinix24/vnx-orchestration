"""Tests for quality_tier discriminator and per-task min/max gates (PR-SR-FIX-3)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from smart_router import _compute_quality_tier, recommend


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


def _plain_task(models: list) -> dict:
    return {"routing_by_task": {"01_code_generation": models}}


def _gated_task(models: list, **gates) -> dict:
    node = {"candidates": models, **gates}
    return {"routing_by_task": {"02_code_review": node}}


# ---------------------------------------------------------------------------
# test_quality_tier_computation_from_score — boundary cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected_tier", [
    (4.9, 1),
    (5.0, 2),
    (7.4, 2),
    (7.5, 3),
    (10.0, 3),
    (1.0, 1),
])
def test_quality_tier_computation_from_score(score, expected_tier):
    assert _compute_quality_tier(score, None) == expected_tier


# ---------------------------------------------------------------------------
# test_cost_tier_zero_forces_tier_1
# ---------------------------------------------------------------------------

def test_cost_tier_zero_forces_tier_1(tmp_path):
    p = _write_yaml(tmp_path, _plain_task([
        {"model_id": "local", "composite_score": 9.0, "avg_duration_seconds": 5.0, "cost_tier": 0},
    ]))
    candidates = recommend("01_code_generation", recommendations_path=p)
    assert candidates[0].quality_tier == 1


def test_cost_tier_zero_overrides_any_score():
    assert _compute_quality_tier(9.9, 0) == 1


# ---------------------------------------------------------------------------
# test_explicit_quality_tier_override
# ---------------------------------------------------------------------------

def test_explicit_quality_tier_override(tmp_path):
    p = _write_yaml(tmp_path, _plain_task([
        {"model_id": "special", "composite_score": 8.0, "avg_duration_seconds": 100.0, "quality_tier": 2},
    ]))
    candidates = recommend("01_code_generation", recommendations_path=p)
    assert candidates[0].quality_tier == 2


def test_invalid_explicit_quality_tier_raises(tmp_path):
    p = _write_yaml(tmp_path, _plain_task([
        {"model_id": "bad", "composite_score": 8.0, "avg_duration_seconds": 100.0, "quality_tier": 5},
    ]))
    with pytest.raises(ValueError, match="quality_tier must be 1-3"):
        recommend("01_code_generation", recommendations_path=p)


# ---------------------------------------------------------------------------
# test_min_tier_filter_excludes_lower
# ---------------------------------------------------------------------------

_THREE_TIER_MODELS = [
    {"model_id": "premium", "composite_score": 9.0, "avg_duration_seconds": 90.0},
    {"model_id": "mid", "composite_score": 6.0, "avg_duration_seconds": 60.0},
    {"model_id": "low", "composite_score": 1.0, "avg_duration_seconds": 10.0},
]


def test_min_tier_filter_excludes_lower(tmp_path):
    p = _write_yaml(tmp_path, _gated_task(_THREE_TIER_MODELS, min_quality_tier=3))
    ids = [c.model_id for c in recommend("02_code_review", recommendations_path=p)]
    assert ids == ["premium"]


# ---------------------------------------------------------------------------
# test_max_tier_filter_caps_premium
# ---------------------------------------------------------------------------

def test_max_tier_filter_caps_premium(tmp_path):
    p = _write_yaml(tmp_path, _gated_task(_THREE_TIER_MODELS, max_quality_tier=2))
    ids = [c.model_id for c in recommend("02_code_review", recommendations_path=p)]
    assert "premium" not in ids
    assert "mid" in ids
    assert "low" in ids


# ---------------------------------------------------------------------------
# test_plain_list_no_filter_no_regression
# ---------------------------------------------------------------------------

def test_plain_list_no_filter_no_regression(tmp_path):
    p = _write_yaml(tmp_path, _plain_task(_THREE_TIER_MODELS))
    candidates = recommend("01_code_generation", recommendations_path=p)
    assert len(candidates) == 3
    by_id = {c.model_id: c for c in candidates}
    assert by_id["premium"].quality_tier == 3
    assert by_id["mid"].quality_tier == 2
    assert by_id["low"].quality_tier == 1


# ---------------------------------------------------------------------------
# test_recommend_returns_tier_on_candidate — real YAML
# ---------------------------------------------------------------------------

def test_recommend_returns_tier_on_candidate():
    candidates = recommend("02_code_review")
    assert len(candidates) > 0
    assert candidates[0].quality_tier == 3
    for c in candidates:
        assert c.quality_tier == 3, (
            f"{c.model_id} has quality_tier={c.quality_tier}, expected 3"
        )
