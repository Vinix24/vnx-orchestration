"""Tests for tier_routing — constraint enforcement and route resolution (PR-2).

Covers: kimi-via-cli-only, deepseek-harness-subscription-blocked, default-off
VNX_AUTO_ROUTE flag, and route_dispatch() wiring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO
from providers.smart_router.tier_routing import TierRoute, resolve_tier_route


def test_tier_zero_uses_local_gemma():
    route = resolve_tier_route(TIER_ZERO, env={})
    assert route.provider == "local-gemma"
    assert route.lane == "mlx"
    assert route.fallback is not None
    assert route.fallback.provider == "ollama"


def test_tier_mid_uses_sonnet():
    route = resolve_tier_route(TIER_MID, env={})
    assert route.provider == "claude"
    assert route.model == "claude-sonnet-4-6"


def test_tier_high_uses_opus():
    route = resolve_tier_route(TIER_HIGH, env={})
    assert route.provider == "claude"
    assert route.model == "claude-opus-4-8"


def test_tier_low_no_key_uses_kimi_cli():
    """Without DEEPSEEK_API_KEY, tier-low uses Kimi CLI (kimi-via-cli-only)."""
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "kimi"
    assert route.lane == "kimi_cli"  # kimi-via-cli-only: never api/moonshot


def test_kimi_lane_never_api_or_moonshot():
    """Kimi provider must always use kimi_cli lane, never api or moonshot."""
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "kimi"
    assert "api" not in route.lane
    assert "moonshot" not in route.lane


def test_tier_low_with_deepseek_key_uses_harness():
    """With DEEPSEEK_API_KEY, tier-low uses DeepSeek claude_harness_keyed."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.provider == "deepseek"
    assert route.lane == "claude_harness_keyed"
    assert "DEEPSEEK_API_KEY" in route.env_requirements


def test_deepseek_harness_blocked_without_key():
    """Empty DEEPSEEK_API_KEY falls back to Kimi (subscription route blocked)."""
    route = resolve_tier_route(TIER_LOW, env={"DEEPSEEK_API_KEY": ""})
    assert route.provider == "kimi"


def test_deepseek_harness_fallback_is_kimi():
    """DeepSeek harness route's fallback is Kimi CLI."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.fallback is not None
    assert route.fallback.provider == "kimi"
    assert route.fallback.lane == "kimi_cli"


def test_unknown_tier_defaults_to_opus():
    """Unknown tier strings default to tier-high (safe over silent skip)."""
    route = resolve_tier_route("tier-unknown", env={})
    assert route.model == "claude-opus-4-8"


def test_route_dispatch_default_off():
    """route_dispatch() returns None when VNX_AUTO_ROUTE is not set."""
    from providers.smart_router import route_dispatch

    result = route_dispatch({"instruction": "add function"}, ["x.py"], 50, env={})
    assert result is None


def test_route_dispatch_auto_route_enabled():
    """route_dispatch() returns a TierRoute when VNX_AUTO_ROUTE=1."""
    from providers.smart_router import route_dispatch

    env = {"VNX_AUTO_ROUTE": "1"}
    result = route_dispatch({"instruction": "add function"}, ["x.py"], 50, env=env)
    assert result is not None
    assert isinstance(result, TierRoute)
    assert result.tier == TIER_LOW


def test_route_dispatch_high_loc():
    """route_dispatch() with LOC=350 → tier-high → Opus."""
    from providers.smart_router import route_dispatch

    env = {"VNX_AUTO_ROUTE": "1"}
    result = route_dispatch({"instruction": "implement feature"}, ["x.py"], 350, env=env)
    assert result is not None
    assert result.tier == TIER_HIGH
    assert result.model == "claude-opus-4-8"
