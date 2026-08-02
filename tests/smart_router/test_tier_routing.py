"""Tests for tier_routing — constraint enforcement and route resolution (PR-2).

Covers: codex-as-vangnet (OI-940), deepseek-harness-subscription-blocked,
default-on router (2026-08-02), and route_dispatch() wiring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO
from providers.smart_router.tier_routing import TierRoute, resolve_tier_route


def test_tier_zero_uses_codex_when_no_key():
    """tier-zero without DEEPSEEK_API_KEY falls back to Codex (was local-gemma;
    local models skipped per operator decision 2026-08-02, pending
    gemma-4-12b-integration)."""
    route = resolve_tier_route(TIER_ZERO, env={})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert route.lane == "provider"


def test_tier_zero_uses_deepseek_with_key():
    """tier-zero with DEEPSEEK_API_KEY uses deepseek-v4-flash via claude-harness."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_ZERO, env=env)
    assert route.provider == "deepseek"
    assert route.model == "deepseek-v4-flash"
    assert route.lane == "claude_harness_keyed"
    assert "DEEPSEEK_API_KEY" in route.env_requirements
    assert route.fallback is not None
    assert route.fallback.provider == "codex"
    assert route.fallback.model == "gpt-5.5"


def test_tier_zero_deepseek_fallback_is_codex():
    """tier-zero deepseek route's fallback is Codex."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_ZERO, env=env)
    assert route.fallback is not None
    assert route.fallback.provider == "codex"
    assert route.fallback.model == "gpt-5.5"


def test_tier_mid_uses_sonnet():
    route = resolve_tier_route(TIER_MID, env={})
    assert route.provider == "claude"
    # canonical registry key (model-ssot-en-ketenlink) — the old claude-sonnet-4-6
    # string is not a wave7_models.yaml key and rejected with
    # model-not-in-current-registry the moment tier-mid actually routes.
    assert route.model == "sonnet-5"


def test_tier_high_uses_opus():
    route = resolve_tier_route(TIER_HIGH, env={})
    assert route.provider == "claude"
    # canonical registry key — the fleet runs Opus 5 (model-ssot-en-ketenlink).
    assert route.model == "opus-5"


def test_tier_low_no_key_uses_codex():
    """Without DEEPSEEK_API_KEY, both tier-zero and tier-low fall back to Codex
    (kimi quota exhausted 2026-08-02, OI-940)."""
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert route.lane == "provider"


def test_tier_low_codex_fallback_is_provider_lane():
    """Codex vangnet routes via the provider lane (dispatch_envelope.ProviderAdapter)."""
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "codex"
    assert route.lane == "provider"


def test_tier_low_with_deepseek_key_uses_harness():
    """With DEEPSEEK_API_KEY, tier-low uses DeepSeek claude_harness_keyed."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.provider == "deepseek"
    assert route.lane == "claude_harness_keyed"
    assert "DEEPSEEK_API_KEY" in route.env_requirements


def test_tier_low_deepseek_route_uses_v4_flash():
    """tier-low DeepSeek route must use deepseek-v4-flash — deepseek-chat was
    discontinued by the provider on 2026-07-24."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.provider == "deepseek"
    assert route.model == "deepseek-v4-flash"


def test_deepseek_harness_blocked_without_key():
    """Empty DEEPSEEK_API_KEY falls back to Codex (kimi quota exhausted, OI-940)."""
    route = resolve_tier_route(TIER_LOW, env={"DEEPSEEK_API_KEY": ""})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"


def test_deepseek_harness_fallback_is_codex():
    """DeepSeek harness route's fallback is Codex (was Kimi, OI-940)."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.fallback is not None
    assert route.fallback.provider == "codex"
    assert route.fallback.model == "gpt-5.5"
    assert route.fallback.lane == "provider"


def test_unknown_tier_defaults_to_opus():
    """Unknown tier strings default to tier-high (safe over silent skip)."""
    route = resolve_tier_route("tier-unknown", env={})
    assert route.model == "opus-5"


def test_route_dispatch_disabled_via_flag():
    """route_dispatch() returns None when VNX_SMART_ROUTER_DISABLE=1."""
    from providers.smart_router import route_dispatch

    env = {"VNX_SMART_ROUTER_DISABLE": "1"}
    result = route_dispatch({"instruction": "add function"}, ["x.py"], 50, env=env)
    assert result is None


def test_route_dispatch_disabled_via_legacy_flag():
    """route_dispatch() returns None when VNX_AUTO_ROUTE=0 (legacy opt-in default-off)."""
    from providers.smart_router import route_dispatch

    env = {"VNX_AUTO_ROUTE": "0"}
    result = route_dispatch({"instruction": "add function"}, ["x.py"], 50, env=env)
    assert result is None


def test_route_dispatch_default_on():
    """route_dispatch() returns a TierRoute by default (no flag needed since 2026-08-02)."""
    from providers.smart_router import route_dispatch

    result = route_dispatch({"instruction": "add function"}, ["x.py"], 50, env={})
    assert result is not None
    assert isinstance(result, TierRoute)
    assert result.tier == TIER_LOW


def test_route_dispatch_auto_route_enabled():
    """route_dispatch() returns a TierRoute when VNX_AUTO_ROUTE=1 (backward compat)."""
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
    assert result.model == "opus-5"


# ---------------------------------------------------------------------------
# door_routing — resolve_door_route / apply_door_route
# ---------------------------------------------------------------------------


def test_door_route_explicit_provider_overrules_router():
    """An explicit provider+model in the spec must win over the router
    (worker-provider-free-choice, pin_semantics=default)."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.CODEX,
        spec_model="gpt-5.5",
        target_slot="T1",
        instruction_text="add a function",
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result is None, "explicit provider must skip router entirely"


def test_door_route_t0_never_routed():
    """T0 is never routed — t0-opus-only is a floor, not an advisory."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T0",
        instruction_text="plan a dispatch",
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result is None, "T0 must never be routed"


def test_door_route_auto_fills_provider():
    """With provider=AUTO and DEEPSEEK_API_KEY present, tier-low resolves to
    deepseek-harness."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=50,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result is not None
    provider, model, reason = result
    assert provider == Provider.DEEPSEEK_HARNESS
    assert model == "deepseek-v4-flash"
    assert "tier=tier-low" in reason


def test_door_route_auto_fallback_codex():
    """Without DEEPSEEK_API_KEY, tier-low falls back to codex, not kimi
    (OI-940 kimi quota exhausted)."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=50,
        env={},  # no DEEPSEEK_API_KEY
    )
    assert result is not None
    provider, model, reason = result
    assert provider == Provider.CODEX, "tier-low without DEEPSEEK_API_KEY must fall back to codex"
    assert model == "gpt-5.5"
    assert "codex" in reason.lower()


def test_door_route_disabled_via_flag():
    """VNX_SMART_ROUTER_DISABLE=1 suppresses the router entirely."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a function",
        env={"VNX_SMART_ROUTER_DISABLE": "1", "DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result is None, "router must be suppressible"


def test_door_route_fail_open_on_broken_input():
    """A broken/missing instruction must not crash the router — fail-open."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    # Empty instruction text and zero LOC still classifies (tier-zero or tier-low),
    # but the router must not raise.
    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="",
        env={},
    )
    # Empty instruction with 0 LOC classifies to tier-low; we get a route or
    # fail-open.
    assert result is not None or result is None  # never raises
