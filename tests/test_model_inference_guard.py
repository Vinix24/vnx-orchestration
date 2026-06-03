#!/usr/bin/env python3
"""Regression tests for model_inference_guard — confounded model-routing inference.

Covers the nightly-digest defect of 2026-06-03: the loop concluded "opus bad at
debugging -> route debugging to sonnet" (95% confidence) from confounded data, where

  - opus ran the HARD tasks (high token, ~49min, T0 orchestrator investigations) and
  - its "errors" were recoverable system/infra failures it DIAGNOSED (resilience).

The guard must refuse such a verdict and return insufficient_comparable_data.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from model_inference_guard import (  # noqa: E402
    INSUFFICIENT,
    HINT,
    MIN_COMPARABLE_SAMPLE,
    difficulty_bucket,
    evaluate_activity_routing,
    routing_hints,
)


def _sessions(n, tokens, *, reasoning_error=None, has_error_recovery=False):
    return [
        {
            "total_output_tokens": tokens,
            "duration_minutes": 49 if tokens > 200_000 else 5,
            "has_error_recovery": has_error_recovery,
            "reasoning_error": reasoning_error,
        }
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Defect A.4 — the core regression
# ---------------------------------------------------------------------------

def test_confounded_opus_hard_vs_sonnet_easy_yields_insufficient():
    """opus=hard-bucket high-token + recoverable system errors, sonnet=easy-bucket.

    The analyzer must NOT recommend routing debugging away from opus.
    """
    sessions_by_model = {
        # opus: 12 heavy investigation sessions, each recovered from a system error
        "claude-opus": _sessions(12, 1_817_000, has_error_recovery=True),
        # sonnet: 12 routine sessions, no errors
        "claude-sonnet": _sessions(12, 123_000, has_error_recovery=False),
    }

    result = evaluate_activity_routing("debugging", sessions_by_model)

    assert result["status"] == INSUFFICIENT
    # Must never name opus as a model to avoid / route away from.
    assert "avoid_model" not in result
    assert result.get("recommended_model") is None or "recommended_model" not in result


def test_routing_hints_drops_confounded_activity():
    """routing_hints() returns no hint at all for the confounded scenario."""
    activity_sessions = {
        "debugging": {
            "claude-opus": _sessions(12, 1_817_000, has_error_recovery=True),
            "claude-sonnet": _sessions(12, 123_000, has_error_recovery=False),
        }
    }
    assert routing_hints(activity_sessions) == []


def test_error_recovery_alone_never_blames_model():
    """Even within the SAME difficulty bucket, has_error_recovery is not model-blame."""
    sessions_by_model = {
        "claude-opus": _sessions(10, 300_000, has_error_recovery=True),
        "claude-sonnet": _sessions(10, 300_000, has_error_recovery=False),
    }
    result = evaluate_activity_routing("debugging", sessions_by_model)
    # Same bucket -> Gate A passes, but no infra-excluded reasoning signal -> insufficient.
    assert result["status"] == INSUFFICIENT
    assert "infra" in result["reason"].lower() or "reasoning" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Gate behaviour
# ---------------------------------------------------------------------------

def test_below_min_sample_is_insufficient():
    sessions_by_model = {
        "claude-opus": _sessions(3, 300_000, reasoning_error=True),
        "claude-sonnet": _sessions(3, 300_000, reasoning_error=False),
    }
    result = evaluate_activity_routing("coding", sessions_by_model)
    assert result["status"] == INSUFFICIENT


def test_clean_signal_same_bucket_meaningful_gap_emits_hint():
    """With an infra-excluded reasoning signal, comparable difficulty, and a real gap,
    the guard DOES emit a hint — proving the guard isn't merely always-insufficient."""
    sessions_by_model = {
        # within same 'substantial' bucket; sonnet has many reasoning errors, opus few
        "claude-opus": _sessions(MIN_COMPARABLE_SAMPLE, 300_000, reasoning_error=False),
        "claude-sonnet": (
            _sessions(MIN_COMPARABLE_SAMPLE - 2, 300_000, reasoning_error=True)
            + _sessions(2, 300_000, reasoning_error=False)
        ),
    }
    result = evaluate_activity_routing("coding", sessions_by_model)
    assert result["status"] == HINT
    assert result["recommended_model"] == "claude-opus"
    assert result["avoid_model"] == "claude-sonnet"
    assert result["bucket"] == "substantial"


def test_clean_signal_but_small_gap_is_insufficient():
    sessions_by_model = {
        "claude-opus": _sessions(MIN_COMPARABLE_SAMPLE, 300_000, reasoning_error=False),
        "claude-sonnet": (
            _sessions(1, 300_000, reasoning_error=True)
            + _sessions(MIN_COMPARABLE_SAMPLE - 1, 300_000, reasoning_error=False)
        ),
    }
    result = evaluate_activity_routing("coding", sessions_by_model)
    assert result["status"] == INSUFFICIENT


def test_difficulty_bucket_separates_heavy_from_routine():
    assert difficulty_bucket(1_817_000) == "heavy"
    assert difficulty_bucket(123_000) == "routine"
    assert difficulty_bucket(10_000) == "trivial"
    assert difficulty_bucket(0) == "trivial"
    assert difficulty_bucket(None) == "trivial"
    # The confound: opus-heavy and sonnet-routine never share a bucket.
    assert difficulty_bucket(1_817_000) != difficulty_bucket(123_000)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
