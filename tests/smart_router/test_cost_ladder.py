"""Tests for the cost-ordered escalation ladder (OI-1221).

Pins the two invariants that made the old tier map a safety net rather than a
ladder, and the two mechanisms that must stay apart:

Ladder (derived from wave7_models.yaml, never Python literals):
  - cost is strictly increasing along the tier order (a higher rung is
    demonstrably more expensive),
  - no two tiers resolve to the same primary (a duplicate rung escalates
    nowhere).

Two mechanisms, opposite in intent:
  - AVAILABILITY fallback (resolve_tier_route) skips an unavailable lane and
    lands ON THE SAME TIER — a safety net, never an escalation.
  - QUALITY escalation (escalate_tier) fires a followup dispatch one tier UP
    (tier_to = tier_from + 1), linked by parent_dispatch — it climbs, never
    walks the fallback chain.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.provider_registry import RegistryLookupError, load_tier_ladder
from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO
from providers.smart_router.tier_routing import escalate_tier, next_tier, resolve_tier_route


def _force_kimi(monkeypatch, present: bool):
    """Deterministically set the kimi CLI presence on PATH (mirrors tier_routing tests)."""
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil,
        "which",
        (lambda name: "/usr/local/bin/kimi") if present else (lambda name: None),
    )


# The full cost ladder (OI-1229): every dispatchable model, cheapest first. The
# four scope buckets (TIER_*) are the classifier's entry rungs; the three
# model-named rungs are escalation destinations only — reachable by climbing,
# never by classification.
LADDER_ORDER = [
    TIER_ZERO,
    TIER_LOW,
    "kimi-k3",
    "gpt-5.5",
    TIER_MID,
    TIER_HIGH,
    "fable-5",
]


# ---------------------------------------------------------------------------
# Ladder invariants — derived from the registry
# ---------------------------------------------------------------------------

def test_ladder_cost_is_strictly_monotonic():
    """A higher tier is demonstrably more expensive (cost strictly increasing)."""
    ladder = load_tier_ladder()
    costs = [rung.output_cost_per_mtok for rung in ladder]
    assert len(costs) == len(set(costs)), f"cost ties are not a ladder: {costs}"
    assert all(a < b for a, b in zip(costs, costs[1:])), costs


def test_ladder_has_no_duplicate_rungs():
    """No two tiers resolve to the same primary (provider, model)."""
    ladder = load_tier_ladder()
    primaries = [(rung.provider, rung.model) for rung in ladder]
    assert len(primaries) == len(set(primaries)), primaries


def test_ladder_tier_zero_and_low_are_distinct():
    """tier-zero and tier-low were identical (both flash); now they differ."""
    by_tier = {rung.tier: rung for rung in load_tier_ladder()}
    assert by_tier[TIER_ZERO].model == "deepseek-v4-flash"
    assert by_tier[TIER_LOW].model == "deepseek-v4-pro"
    assert by_tier[TIER_ZERO].model != by_tier[TIER_LOW].model


def test_ladder_order_is_cheapest_first():
    """The authored tier order climbs from the cheapest to the most expensive."""
    ladder = load_tier_ladder()
    assert [rung.tier for rung in ladder] == LADDER_ORDER


def test_ladder_covers_every_dispatchable_model():
    """Seven rungs — one per dispatchable model — in strict cost order (OI-1229)."""
    ladder = load_tier_ladder()
    assert [(r.tier, r.model, r.output_cost_per_mtok) for r in ladder] == [
        (TIER_ZERO, "deepseek-v4-flash", 0.28),
        (TIER_LOW, "deepseek-v4-pro", 0.87),
        ("kimi-k3", "kimi-k3", 2.50),
        ("gpt-5.5", "gpt-5.5", 10.00),
        (TIER_MID, "sonnet-5", 15.00),
        (TIER_HIGH, "opus-5", 25.00),
        ("fable-5", "fable-5", 50.00),
    ]


# ---------------------------------------------------------------------------
# Fail-loud: a drifted registry is caught at load, not silently
# ---------------------------------------------------------------------------

def _model(out_cost: float) -> dict:
    return {
        "litellm_name": "m",
        "cost_input_per_mtok": 0.1,
        "cost_output_per_mtok": out_cost,
        "max_tokens": 100,
        "supports_streaming": True,
        "supports_tool_calls": True,
    }


def _provider(enum: str, models: dict) -> dict:
    return {
        "enabled": True,
        "api_key_env": "A",
        "dispatch_enum": enum,
        "models": models,
    }


def _write_registry(tmp_path: Path, providers: dict, tier_map: dict) -> Path:
    path = tmp_path / "wave7_models.yaml"
    # sort_keys=False: the tier_map dict order IS the authored ladder order under
    # test (a sorted dump would silently reorder tier-zero/tier-low and turn the
    # non-monotonic fixture into a valid ladder).
    path.write_text(
        yaml.dump(
            {"providers": providers, "routing": {"tier_map": tier_map}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_load_tier_ladder_fails_on_duplicate_rung(tmp_path):
    """Two tiers resolving to the same primary raise — escalation would change nothing."""
    providers = {
        "pa": _provider("pa", {"cheap": _model(0.5)}),
        "pb": _provider("pb", {"pricey": _model(5.0)}),
    }
    tier_map = {
        "tier-zero": {"provider": "pa", "model": "cheap", "lane": "l"},
        "tier-low": {"provider": "pa", "model": "cheap", "lane": "l"},
    }
    path = _write_registry(tmp_path, providers, tier_map)
    with pytest.raises(RegistryLookupError, match="duplicate rungs"):
        load_tier_ladder(registry_path=path)


def test_load_tier_ladder_fails_on_equal_cost(tmp_path):
    """Two distinct primaries at the same cost raise — no strict climb."""
    providers = {
        "pa": _provider("pa", {"cheap": _model(0.5)}),
        "pc": _provider("pc", {"samecost": _model(0.5)}),
    }
    tier_map = {
        "tier-zero": {"provider": "pa", "model": "cheap", "lane": "l"},
        "tier-low": {"provider": "pc", "model": "samecost", "lane": "l"},
    }
    path = _write_registry(tmp_path, providers, tier_map)
    with pytest.raises(RegistryLookupError, match="strict cost ladder"):
        load_tier_ladder(registry_path=path)


def test_load_tier_ladder_fails_on_non_monotonic_cost(tmp_path):
    """A higher tier that is CHEAPER than the tier below raises."""
    providers = {
        "pa": _provider("pa", {"cheap": _model(0.5)}),
        "pb": _provider("pb", {"pricey": _model(5.0)}),
    }
    tier_map = {
        "tier-zero": {"provider": "pb", "model": "pricey", "lane": "l"},  # 5.0
        "tier-low": {"provider": "pa", "model": "cheap", "lane": "l"},    # 0.5
    }
    path = _write_registry(tmp_path, providers, tier_map)
    with pytest.raises(RegistryLookupError, match="strict cost ladder"):
        load_tier_ladder(registry_path=path)


# ---------------------------------------------------------------------------
# Quality escalation — rejected result climbs one tier up
# ---------------------------------------------------------------------------

def test_escalate_rejected_result_climbs_one_tier():
    esc = escalate_tier(TIER_LOW, "20260815-rejected-attempt")
    assert esc.tier_from == TIER_LOW
    assert esc.tier_to == "kimi-k3"
    assert esc.parent_dispatch == "20260815-rejected-attempt"


def test_escalate_from_zero_to_low():
    assert escalate_tier(TIER_ZERO, "d-zero").tier_to == TIER_LOW


def test_escalate_from_mid_to_high():
    assert escalate_tier(TIER_MID, "d-mid").tier_to == TIER_HIGH


def test_escalate_at_top_returns_none():
    assert escalate_tier("fable-5", "d-top").tier_to is None


def test_escalate_unknown_tier_returns_none():
    assert escalate_tier("tier-unknown", "d-unknown").tier_to is None


def test_escalate_model_named_rungs_climb_in_cost_order():
    """The three escalation-only rungs slot into the climb at their cost position."""
    assert escalate_tier("kimi-k3", "d1").tier_to == "gpt-5.5"
    assert escalate_tier("gpt-5.5", "d2").tier_to == TIER_MID
    assert escalate_tier(TIER_HIGH, "d3").tier_to == "fable-5"
    assert escalate_tier("fable-5", "d4").tier_to is None


def test_next_tier_matches_escalate_tier():
    assert next_tier(TIER_ZERO) == TIER_LOW
    assert next_tier(TIER_LOW) == "kimi-k3"
    assert next_tier("kimi-k3") == "gpt-5.5"
    assert next_tier("gpt-5.5") == TIER_MID
    assert next_tier(TIER_MID) == TIER_HIGH
    assert next_tier(TIER_HIGH) == "fable-5"
    assert next_tier("fable-5") is None


# ---------------------------------------------------------------------------
# The two mechanisms are opposite — availability stays, escalation climbs
# ---------------------------------------------------------------------------

def test_availability_fallback_stays_on_same_tier(monkeypatch):
    """An unavailable lane walks the fallback chain but stays on the SAME tier."""
    _force_kimi(monkeypatch, present=False)
    route = resolve_tier_route(TIER_LOW, env={})  # no DEEPSEEK_API_KEY, no kimi
    assert route.provider == "codex"   # fell back to the vangnet lane
    assert route.tier == TIER_LOW      # but did NOT climb — availability is not escalation


def test_quality_escalation_climbs_where_fallback_stays(monkeypatch):
    """Availability fallback keeps the tier; quality escalation moves up one rung."""
    _force_kimi(monkeypatch, present=False)
    route = resolve_tier_route(TIER_LOW, env={})
    assert route.tier == TIER_LOW

    esc = escalate_tier(TIER_LOW, "d-rejected")
    assert esc.tier_from == TIER_LOW
    assert esc.tier_to == "kimi-k3"
    assert esc.parent_dispatch == "d-rejected"
