#!/usr/bin/env python3
"""tests/test_subprocess_cheap_lane.py — Cheap-lane delegation in subprocess_dispatch.

Proves that when routing_policy decides a non-Claude (cheap) lane:
  1. _select_dispatch_path returns (lane, current_model) — NOT (None, claude_fallback).
  2. The caller (provider_dispatch) is expected to execute the non-Claude provider.

The critical regression: the OLD code fell back to the first Claude model in
fallback_chain when the lane was non-Claude.  These tests assert that behaviour
is GONE: cheap_lane_provider is the non-Claude lane string, not None.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import _select_dispatch_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(**kwargs: str) -> dict:
    return dict(kwargs)


def _enabled(**extra: str) -> dict:
    return {"VNX_ROUTING_POLICY_ENABLED": "1", **extra}


# ---------------------------------------------------------------------------
# Routing disabled — guards short-circuit before decide_lane is called
# ---------------------------------------------------------------------------


class TestSelectDispatchPathDisabled:
    def test_no_env_var_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_env(),  # VNX_ROUTING_POLICY_ENABLED absent
        )
        assert cheap is None
        assert model == "sonnet"

    def test_env_var_zero_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_env(VNX_ROUTING_POLICY_ENABLED="0"),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_empty_task_class_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_auto_route_applied_skips_routing(self):
        """When smart_router already ran, routing_policy must not override it."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="haiku",
            auto_route_applied=True,
            env=_enabled(),
        )
        assert cheap is None
        assert model == "haiku"  # unchanged — smart_router decision preserved


# ---------------------------------------------------------------------------
# Claude lanes — cheap_lane_provider is None; effective_model is updated
# ---------------------------------------------------------------------------


class TestSelectDispatchPathClaudeLane:
    def test_claude_sonnet_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/sonnet-4-6",
            rule_name="refactor-code-default",
            rationale="test",
            fallback_chain=[],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="medium",
                current_model="opus",  # should be overridden
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"

    def test_claude_haiku_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/haiku-4-5",
            rule_name="simple-cleanup",
            rationale="test",
            fallback_chain=["claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="lint-narrow",
                complexity="low",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "haiku"

    def test_claude_opus_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/opus",
            rule_name="research-deep",
            rationale="test",
            fallback_chain=[],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="research",
                complexity="high",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "opus"


# ---------------------------------------------------------------------------
# Non-Claude (cheap) lanes — THE critical regression guard
# ---------------------------------------------------------------------------


class TestSelectDispatchPathCheapLane:
    """Non-Claude lanes must return (lane, current_model), never (None, claude_fallback).

    The OLD code found the first Claude model in fallback_chain and set _effective_model
    to it, silently routing the dispatch through Claude instead of the chosen provider.
    These tests prove that behaviour is eliminated.
    """

    def test_litellm_moonshot_lane_not_claude_fallback(self):
        """code-review → kimi/moonshot: cheap_lane_provider set, Claude NOT used."""
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="litellm:moonshot:kimi-k2-0905-default",
            rule_name="review-analysis",
            rationale="test",
            # fallback_chain contains Claude — old code would have used this as _effective_model!
            fallback_chain=["litellm:deepseek:deepseek-v4-pro", "claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="code-review",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )

        # cheap_lane_provider MUST be the lane — not None (old fallback was None + Claude model)
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        # effective_model is unchanged — provider_dispatch resolves its own model
        assert model == "sonnet"

    def test_litellm_deepseek_lane_not_claude_fallback(self):
        """DeepSeek lane: cheap_lane_provider set, Claude model NOT used."""
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="litellm:deepseek:deepseek-v4-pro",
            rule_name="cost-optimized-code",
            rationale="test",
            fallback_chain=["litellm:moonshot:kimi-k2-0905-default", "claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )

        assert cheap == "litellm:deepseek:deepseek-v4-pro"
        assert model == "sonnet"  # NOT overridden to a Claude model

    def test_current_model_preserved_unchanged_for_various_inputs(self):
        """Whatever current_model is, it passes through unmodified when lane is cheap."""
        from routing_policy import RoutingDecision

        for original_model in ("sonnet", "opus", "haiku", "claude-sonnet-4-6"):
            decision = RoutingDecision(
                lane="litellm:moonshot:kimi-k2-0905-default",
                rule_name="review-analysis",
                rationale="test",
                fallback_chain=["claude/sonnet-4-6"],
            )
            with patch("routing_policy.decide_lane", return_value=decision):
                cheap, model = _select_dispatch_path(
                    task_class="code-review",
                    complexity="medium",
                    current_model=original_model,
                    env=_enabled(),
                )
            assert cheap is not None, f"cheap_lane_provider unset for current_model={original_model!r}"
            assert model == original_model, (
                f"current_model={original_model!r} was unexpectedly changed to {model!r}"
            )

    def test_cheap_lane_provider_is_full_lane_string(self):
        """cheap_lane_provider equals the full lane string, ready for --provider arg."""
        from routing_policy import RoutingDecision

        lane = "litellm:moonshot:kimi-k2-0905-default"
        decision = RoutingDecision(lane=lane, rule_name="r", rationale="t", fallback_chain=[])
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, _ = _select_dispatch_path(
                task_class="analysis",
                complexity="low",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap == lane  # passed verbatim to provider_dispatch --provider


# ---------------------------------------------------------------------------
# Error handling — any failure falls back to (None, current_model)
# ---------------------------------------------------------------------------


class TestSelectDispatchPathErrors:
    def test_decide_lane_file_not_found_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=FileNotFoundError("no policy")):
            cheap, model = _select_dispatch_path(
                task_class="code-review",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"

    def test_decide_lane_value_error_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=ValueError("bad yaml")):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="high",
                current_model="opus",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "opus"

    def test_decide_lane_runtime_error_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=RuntimeError("unexpected")):
            cheap, model = _select_dispatch_path(
                task_class="research",
                complexity="high",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"


# ---------------------------------------------------------------------------
# Production policy smoke tests — against the real routing_policy.yaml
# ---------------------------------------------------------------------------


class TestSelectDispatchPathProductionPolicy:
    """Exercises the real routing_policy.yaml to catch yaml drift."""

    def test_code_review_routes_to_cheap_moonshot_lane(self):
        """code-review → litellm:moonshot (cheap); must NOT produce Claude."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        assert model == "sonnet"  # unchanged

    def test_refactor_medium_routes_to_claude_sonnet(self):
        """refactor + medium → claude/sonnet-4-6 (cheap is None, model overridden)."""
        cheap, model = _select_dispatch_path(
            task_class="refactor",
            complexity="medium",
            current_model="opus",  # should be overridden
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_lint_narrow_low_routes_to_claude_haiku(self):
        """lint-narrow + low → claude/haiku-4-5 (cheap is None)."""
        cheap, model = _select_dispatch_path(
            task_class="lint-narrow",
            complexity="low",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "haiku"

    def test_analysis_routes_to_cheap_moonshot_lane(self):
        """analysis → litellm:moonshot (cheap); must NOT produce Claude."""
        cheap, model = _select_dispatch_path(
            task_class="analysis",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        assert model == "sonnet"

    def test_unknown_task_class_returns_claude_default(self):
        """Unknown task_class hits default lane (claude/sonnet-4-6)."""
        cheap, model = _select_dispatch_path(
            task_class="unknown-task-xyz",
            complexity="medium",
            current_model="haiku",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"  # default_lane mapped to sonnet
