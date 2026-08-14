"""Tests for tier_routing — registry-driven routes and fallback-chain walking.

Covers (ADR-036 + OI-1185):
- model identity and provider strings come from wave7_models.yaml (no Python
  literals on the routing path); tier-mid/tier-high resolve to claude
- the fallback chain is walked at decision time: a primary lane that is
  unavailable (missing key, CLI absent, or cooldown) is skipped and the next
  step takes over; missing key and cooldown follow the SAME chain
- codex is the terminal vangnet; claude is the ungated mid/high lane
- door_routing fails loud (RegistryLookupError) on an unknown provider
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO
from providers.smart_router.tier_routing import TierRoute, resolve_tier_route


def _force_kimi(monkeypatch, present: bool):
    """Deterministically set the kimi CLI presence on PATH."""
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil,
        "which",
        (lambda name: "/usr/local/bin/kimi") if present else (lambda name: None),
    )


# ---------------------------------------------------------------------------
# Registry-driven provider/model identity (ADR-036)
# ---------------------------------------------------------------------------

def test_tier_zero_uses_codex_when_no_key_and_no_kimi(monkeypatch):
    """tier-zero without DEEPSEEK_API_KEY and no kimi CLI falls back to Codex."""
    _force_kimi(monkeypatch, present=False)
    route = resolve_tier_route(TIER_ZERO, env={})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert route.lane == "provider"


def test_tier_zero_uses_deepseek_with_key():
    """tier-zero with DEEPSEEK_API_KEY uses deepseek-v4-flash via claude-harness."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_ZERO, env=env)
    assert route.provider == "deepseek-harness"
    assert route.model == "deepseek-v4-flash"
    assert route.lane == "claude_harness_keyed"
    assert "DEEPSEEK_API_KEY" in route.env_requirements
    assert route.fallback is not None
    assert route.fallback.provider == "kimi"
    assert route.fallback.model == "kimi-k3"
    assert route.fallback.fallback is not None
    assert route.fallback.fallback.provider == "codex"
    assert route.fallback.fallback.model == "gpt-5.5"


def test_tier_mid_uses_sonnet():
    route = resolve_tier_route(TIER_MID, env={})
    assert route.provider == "claude"
    assert route.model == "sonnet-5"
    assert route.lane == "tmux_interactive"


def test_tier_high_uses_opus():
    route = resolve_tier_route(TIER_HIGH, env={})
    assert route.provider == "claude"
    assert route.model == "opus-5"


def test_tier_low_no_key_no_kimi_uses_codex(monkeypatch):
    """Without DEEPSEEK_API_KEY and no kimi CLI, tier-low falls back to Codex."""
    _force_kimi(monkeypatch, present=False)
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert route.lane == "provider"


def test_tier_low_with_deepseek_key_uses_harness():
    """With DEEPSEEK_API_KEY, tier-low uses DeepSeek claude_harness_keyed."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.provider == "deepseek-harness"
    assert route.lane == "claude_harness_keyed"
    assert "DEEPSEEK_API_KEY" in route.env_requirements


def test_tier_low_deepseek_route_uses_v4_flash():
    """tier-low DeepSeek route must use deepseek-v4-flash."""
    env = {"DEEPSEEK_API_KEY": "sk-test-123"}
    route = resolve_tier_route(TIER_LOW, env=env)
    assert route.provider == "deepseek-harness"
    assert route.model == "deepseek-v4-flash"


def test_deepseek_harness_blocked_without_key(monkeypatch):
    """Empty DEEPSEEK_API_KEY falls back through the chain (kimi absent -> codex)."""
    _force_kimi(monkeypatch, present=False)
    route = resolve_tier_route(TIER_LOW, env={"DEEPSEEK_API_KEY": ""})
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"


def test_unknown_tier_defaults_to_opus():
    """Unknown tier strings default to tier-high (safe over silent skip)."""
    route = resolve_tier_route("tier-unknown", env={})
    assert route.model == "opus-5"


# ---------------------------------------------------------------------------
# Fallback-chain walking — missing key and cooldown share one chain (OI-1185)
# ---------------------------------------------------------------------------

def test_missing_key_lands_on_kimi_when_kimi_present(monkeypatch):
    """Missing DEEPSEEK_API_KEY walks the same chain as cooldown: kimi next."""
    _force_kimi(monkeypatch, present=True)
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.provider == "kimi"
    assert route.model == "kimi-k3"
    assert route.lane == "kimi_cli"


def test_primary_quota_failure_lands_on_fallback(tmp_path, monkeypatch):
    """Primary lane in quota cooldown -> fallback lane takes over (OI-1185).

    Without the fallback-chain walk this fails: resolve_tier_route would return
    the primary deepseek-harness route even while it was in cooldown, because
    nothing walked its ``fallback`` field.
    """
    from providers.smart_router.availability import record_lane_failure

    _force_kimi(monkeypatch, present=True)
    state_dir = tmp_path / "state"
    now = 1_000_000.0
    record_lane_failure(
        "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
    )
    route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
    )
    assert route.provider == "kimi"
    assert route.model == "kimi-k3"
    assert route.lane == "kimi_cli"
    assert "deepseek-harness unavailable" in route.reason


def test_missing_key_and_cooldown_follow_same_chain(tmp_path, monkeypatch):
    """Missing key and cooldown both land on kimi — the SAME chain (OI-1185)."""
    from providers.smart_router.availability import record_lane_failure

    _force_kimi(monkeypatch, present=True)
    state_dir = tmp_path / "state"
    now = 5_000_000.0
    record_lane_failure(
        "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
    )

    cooldown_route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
    )
    missing_key_route = resolve_tier_route(
        TIER_LOW, env={}, state_dir=state_dir, now=now,
    )

    assert cooldown_route.provider == "kimi"
    assert missing_key_route.provider == "kimi"
    assert cooldown_route.lane == missing_key_route.lane
    assert cooldown_route.model == missing_key_route.model


def test_missing_key_and_cooldown_both_land_on_codex_when_kimi_absent(tmp_path, monkeypatch):
    """Both causes still share one chain when kimi is absent -> codex vangnet."""
    from providers.smart_router.availability import record_lane_failure

    _force_kimi(monkeypatch, present=False)
    state_dir = tmp_path / "state"
    now = 6_000_000.0
    record_lane_failure(
        "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
    )

    cooldown_route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
    )
    missing_key_route = resolve_tier_route(
        TIER_LOW, env={}, state_dir=state_dir, now=now,
    )

    assert cooldown_route.provider == "codex"
    assert missing_key_route.provider == "codex"


def test_tier_low_deepseek_cooldown_kimi_unavailable_falls_back_to_codex(tmp_path, monkeypatch):
    """DeepSeek in cooldown and kimi CLI absent -> Codex vangnet, reason visible."""
    from providers.smart_router.availability import record_lane_failure

    _force_kimi(monkeypatch, present=False)
    state_dir = tmp_path / "state"
    now = 2_000_000.0
    record_lane_failure(
        "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
    )
    route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
    )
    assert route.provider == "codex"
    assert route.model == "gpt-5.5"
    assert "deepseek-harness unavailable" in route.reason
    assert "kimi unavailable" in route.reason


def test_tier_low_deepseek_reengages_after_cooldown(tmp_path):
    """After the cooldown period the lane re-engages — no release required."""
    from providers.smart_router.availability import record_lane_failure

    state_dir = tmp_path / "state"
    now = 3_000_000.0
    record_lane_failure(
        "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
    )
    route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"},
        state_dir=state_dir, now=now + 3601,
    )
    assert route.provider == "deepseek-harness"
    assert route.model == "deepseek-v4-flash"


def test_auth_failure_keeps_lane_out_past_waiting(tmp_path, monkeypatch):
    """An auth failure is non-recoverable: waiting past the quota window does
    NOT bring the lane back (operator must clear the state)."""
    from providers.smart_router.availability import record_lane_failure

    _force_kimi(monkeypatch, present=True)
    state_dir = tmp_path / "state"
    now = 4_000_000.0
    record_lane_failure(
        "deepseek-harness", "403 auth", state_dir=state_dir, now=now,
    )
    route = resolve_tier_route(
        TIER_LOW, env={"DEEPSEEK_API_KEY": "sk-test"},
        state_dir=state_dir, now=now + 999_999,
    )
    assert route.provider == "kimi"


# ---------------------------------------------------------------------------
# route_dispatch wiring
# ---------------------------------------------------------------------------

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
    """route_dispatch() with LOC=350 -> tier-high -> Opus."""
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
    """An explicit provider+model in the spec must win over the router."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_EXPLICIT_PROVIDER,
        resolve_door_route,
    )

    result = resolve_door_route(
        spec_provider=Provider.CODEX,
        spec_model="gpt-5.5",
        target_slot="T1",
        instruction_text="add a function",
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None, "explicit provider must skip router entirely"
    assert result.decline_reason == DECLINE_EXPLICIT_PROVIDER


def test_door_route_t0_never_routed():
    """T0 is never routed — t0-opus-only is a floor, not an advisory."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_T0_NEVER_ROUTES,
        resolve_door_route,
    )

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T0",
        instruction_text="plan a dispatch",
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None, "T0 must never be routed"
    assert result.decline_reason == DECLINE_T0_NEVER_ROUTES


def test_door_route_auto_fills_provider(monkeypatch):
    """With provider=AUTO and DEEPSEEK_API_KEY present, tier-low resolves to
    deepseek-harness (when the tier-low staging flag is on at full canary)."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_LOW", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=50,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is not None
    provider, model, reason = result.route
    assert provider == Provider.DEEPSEEK_HARNESS
    assert model == "deepseek-v4-flash"
    assert "tier=tier-low" in reason


def test_door_route_auto_fallback_codex(monkeypatch):
    """Without DEEPSEEK_API_KEY and no kimi CLI, tier-low falls back to codex."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    _force_kimi(monkeypatch, present=False)
    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_LOW", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=50,
        env={},  # no DEEPSEEK_API_KEY
    )
    assert result.route is not None
    provider, model, reason = result.route
    assert provider == Provider.CODEX, "tier-low without key/kimi must fall back to codex"
    assert model == "gpt-5.5"
    assert "codex" in reason.lower()


def test_door_route_disabled_via_flag():
    """VNX_SMART_ROUTER_DISABLE=1 suppresses the router entirely."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_ROUTER_DISABLED,
        resolve_door_route,
    )

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a function",
        env={"VNX_SMART_ROUTER_DISABLE": "1", "DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None, "router must be suppressible"
    assert result.decline_reason == DECLINE_ROUTER_DISABLED


def test_door_route_disabled_via_legacy_flag():
    """VNX_AUTO_ROUTE=0 (legacy opt-in default-off) also disables."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_ROUTER_DISABLED,
        resolve_door_route,
    )

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a function",
        env={"VNX_AUTO_ROUTE": "0", "DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None
    assert result.decline_reason == DECLINE_ROUTER_DISABLED


def test_door_route_unknown_provider_fails_loud(monkeypatch):
    """A provider string the registry does not know raises RegistryLookupError
    (ADR-036 §2 fail-loud) — not a silent decline."""
    from dispatch_spec import Provider
    from providers.provider_registry import RegistryLookupError
    from providers.smart_router.door_routing import resolve_door_route
    from providers.smart_router.tier_routing import TierRoute
    import providers.smart_router.tier_routing as tier_routing_module

    monkeypatch.setattr(
        tier_routing_module,
        "resolve_tier_route",
        lambda tier, env, state_dir=None, now=None: TierRoute(
            tier=tier, provider="future-provider", model="future-model", lane="provider",
        ),
    )
    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_LOW", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    with pytest.raises(RegistryLookupError):
        resolve_door_route(
            spec_provider=Provider.AUTO,
            spec_model=None,
            target_slot="T1",
            instruction_text="add a helper function",
            file_paths=["src/foo.py"],
            loc_estimate=50,
            env={},
        )


def test_door_route_classifier_error_declines_with_reason(monkeypatch):
    """A classifier crash declines with classifier-error (fail-open) — never
    raises, never a bare None (OI-1187)."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_CLASSIFIER_ERROR,
        resolve_door_route,
    )
    import providers.smart_router.cost_tier as cost_tier_module

    def boom(task_spec, file_paths, loc_estimate):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(cost_tier_module, "classify_dispatch", boom)

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=50,
        env={},
    )
    assert result.route is None
    assert result.decline_reason == DECLINE_CLASSIFIER_ERROR
    assert result.tier is None


def test_door_route_fail_open_on_broken_input():
    """A broken/missing instruction must not crash the router — fail-open."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="",
        env={},
    )
    assert result is not None


def test_door_route_tier_mid_resolves_claude(monkeypatch):
    """tier-mid now maps to a concrete Provider.CLAUDE choice (enum-gap fix)."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_MID", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="implement a medium feature",
        file_paths=["src/foo.py"],
        loc_estimate=200,
        env={},
    )
    assert result.route is not None
    provider, model, reason = result.route
    assert provider == Provider.CLAUDE
    assert model == "sonnet-5"
    assert "tier=tier-mid" in reason


def test_door_route_tier_high_resolves_claude(monkeypatch):
    """tier-high maps to Provider.CLAUDE (opus-5), not a silent decline."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_HIGH", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="implement a large feature",
        file_paths=["src/foo.py"],
        loc_estimate=350,
        env={},
    )
    assert result.route is not None
    provider, model, reason = result.route
    assert provider == Provider.CLAUDE
    assert model == "opus-5"
    assert "tier=tier-high" in reason


def test_door_route_t0_stays_unrouted_when_mid_tier():
    """T0 is never routed even when the classifier would land on tier-mid."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_T0_NEVER_ROUTES,
        resolve_door_route,
    )

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T0",
        instruction_text="implement a medium feature",
        file_paths=["src/foo.py"],
        loc_estimate=200,
        env={},
    )
    assert result.route is None, "T0 must never be routed"
    assert result.decline_reason == DECLINE_T0_NEVER_ROUTES


# ---------------------------------------------------------------------------
# door_routing — AUTO-staging gate (dispatch 20260814s-a)
# ---------------------------------------------------------------------------

def test_door_route_staging_tier_disabled_declines(monkeypatch):
    """A classified tier that is not enabled declines, naming the tier."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route
    from providers.smart_router.staging import DECLINE_TIER_DISABLED

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_HIGH", "1")  # tier-high on, tier-zero off
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=10,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None
    assert result.decline_reason == f"{DECLINE_TIER_DISABLED}:tier-zero"
    assert result.tier == "tier-zero"


def test_door_route_staging_tier_on_others_off(monkeypatch):
    """tier-zero on while tier-high off: the one routes, the other declines."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route
    from providers.smart_router.staging import DECLINE_TIER_DISABLED

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_ZERO", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    routed = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=10,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert routed.route is not None
    assert routed.tier == "tier-zero"

    declined = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="implement a large feature",
        file_paths=["src/foo.py"],
        loc_estimate=350,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert declined.route is None
    assert declined.decline_reason == f"{DECLINE_TIER_DISABLED}:tier-high"
    assert declined.tier == "tier-high"


def test_door_route_canary_zero_routes_nothing(monkeypatch):
    """An enabled tier at canary 0 declines every dispatch into the control group."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route
    from providers.smart_router.staging import DECLINE_CANARY_CONTROL

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_ZERO", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "0")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=10,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    assert result.route is None
    assert result.decline_reason == f"{DECLINE_CANARY_CONTROL}:tier-zero"


def test_door_route_kill_switch_wins_over_staging(monkeypatch):
    """VNX_SMART_ROUTER_DISABLE wins even when staging is fully open."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import (
        DECLINE_ROUTER_DISABLED,
        resolve_door_route,
    )

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_ZERO", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")

    result = resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=10,
        env={"VNX_SMART_ROUTER_DISABLE": "1"},
    )
    assert result.route is None
    assert result.decline_reason == DECLINE_ROUTER_DISABLED


def test_door_route_canary_same_dispatch_same_group(monkeypatch):
    """The same dispatch re-evaluated falls in the same canary group."""
    from dispatch_spec import Provider
    from providers.smart_router.door_routing import resolve_door_route

    monkeypatch.setenv("VNX_SMART_ROUTER_TIER_ZERO", "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "50")

    kwargs = dict(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="add a helper function",
        file_paths=["src/foo.py"],
        loc_estimate=10,
        env={"DEEPSEEK_API_KEY": "sk-test"},
    )
    first = resolve_door_route(**kwargs)
    second = resolve_door_route(**kwargs)
    assert (first.route is None) == (second.route is None), (
        "the same dispatch must fall in the same canary group on re-evaluation"
    )
    assert first.decline_reason == second.decline_reason
    assert first.route == second.route
