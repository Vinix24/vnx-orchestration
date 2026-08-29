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
    TIER_MID,
    "kimi-k3",
    TIER_HIGH,
    "gpt-5.5",
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
        (TIER_MID, "sonnet-5", 10.00),
        ("kimi-k3", "kimi-k3", 15.00),
        (TIER_HIGH, "opus-5", 25.00),
        ("gpt-5.5", "gpt-5.5", 30.00),
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
    """OI-1356: tier-low climbs to tier-mid (escalation_order), not kimi-k3
    (cost ladder) -- kimi-k3 is excluded from escalation_order."""
    esc = escalate_tier(TIER_LOW, "20260815-rejected-attempt", failure_class="model_error")
    assert esc.tier_from == TIER_LOW
    assert esc.tier_to == TIER_MID
    assert esc.parent_dispatch == "20260815-rejected-attempt"


def test_escalate_from_zero_to_low():
    assert escalate_tier(TIER_ZERO, "d-zero", failure_class="model_error").tier_to == TIER_LOW


def test_escalate_from_mid_to_high():
    assert escalate_tier(TIER_MID, "d-mid", failure_class="model_error").tier_to == TIER_HIGH


def test_escalate_at_top_returns_none():
    assert escalate_tier("fable-5", "d-top", failure_class="model_error").tier_to is None


def test_escalate_unknown_tier_returns_none():
    assert escalate_tier("tier-unknown", "d-unknown", failure_class="model_error").tier_to is None


def test_kimi_k3_and_gpt_5_5_are_not_escalation_destinations():
    """OI-1356: kimi-k3 and gpt-5.5 stay full tier_map rungs (dispatchable,
    fallback-eligible) but are excluded from escalation_order, so they are
    neither a climb source nor a climb destination.

    This assertion changed meaning from the pre-OI-1356 version of this test
    (formerly ``test_escalate_model_named_rungs_climb_in_cost_order``), which
    asserted the three model-named rungs slotted into the climb at their cost
    position (kimi-k3 -> gpt-5.5 -> tier-mid). That coupling -- escalation
    walking the cost ladder instead of an explicit climb order -- is exactly
    the defect OI-1356 removes."""
    assert escalate_tier("kimi-k3", "d1", failure_class="model_error").tier_to is None
    assert escalate_tier("gpt-5.5", "d2", failure_class="model_error").tier_to is None
    assert escalate_tier(TIER_HIGH, "d3", failure_class="model_error").tier_to == "fable-5"
    assert escalate_tier("fable-5", "d4", failure_class="model_error").tier_to is None


def test_next_tier_matches_escalate_tier():
    """OI-1356: the climb follows escalation_order (tier-zero -> tier-low ->
    tier-mid -> tier-high -> fable-5); kimi-k3/gpt-5.5 are not on it."""
    assert next_tier(TIER_ZERO) == TIER_LOW
    assert next_tier(TIER_LOW) == TIER_MID
    assert next_tier(TIER_MID) == TIER_HIGH
    assert next_tier(TIER_HIGH) == "fable-5"
    assert next_tier("fable-5") is None
    assert next_tier("kimi-k3") is None
    assert next_tier("gpt-5.5") is None


# ---------------------------------------------------------------------------
# Escalation decision table (dispatch 20260816-p6-escalate-tier-ds)
# ---------------------------------------------------------------------------

def test_model_error_climbs_one_tier():
    """model_error -> climb one tier (escalation_order: tier-low -> tier-mid)."""
    esc = escalate_tier(TIER_LOW, "d-model", failure_class="model_error")
    assert esc.action == "climb"
    assert esc.tier_to == TIER_MID
    assert esc.notify_operator is False
    assert esc.unknown_class is False


def test_credit_exhausted_climbs_and_notifies_operator():
    """credit_exhausted -> climb one tier AND notify operator."""
    esc = escalate_tier(TIER_LOW, "d-credit", failure_class="credit_exhausted")
    assert esc.action == "climb"
    assert esc.tier_to == TIER_MID
    assert esc.notify_operator is True


def test_auth_rejected_does_not_climb():
    """auth_rejected -> no climb (a higher tier has the same auth problem)."""
    esc = escalate_tier(TIER_LOW, "d-auth", failure_class="auth_rejected")
    assert esc.action == "no_climb"
    assert esc.tier_to is None
    assert esc.notify_operator is False


def test_timeout_retries_same_tier_then_climbs():
    """timeout -> retry the same tier first, then climb on the next rejection."""
    retry = escalate_tier(TIER_LOW, "d-timeout", failure_class="timeout")
    assert retry.action == "retry_same_tier"
    assert retry.tier_to == TIER_LOW  # same rung, not up

    climbed = escalate_tier(TIER_LOW, "d-timeout", failure_class="timeout", retried=True)
    assert climbed.action == "climb"
    assert climbed.tier_to == TIER_MID


def test_empty_completion_retries_same_tier_then_climbs():
    """empty_completion -> retry the same tier first, then climb."""
    retry = escalate_tier(TIER_LOW, "d-empty", failure_class="empty_completion")
    assert retry.action == "retry_same_tier"
    assert retry.tier_to == TIER_LOW

    climbed = escalate_tier(TIER_LOW, "d-empty", failure_class="empty_completion", retried=True)
    assert climbed.action == "climb"
    assert climbed.tier_to == TIER_MID


def test_unknown_class_does_not_climb_and_reports_loud():
    """unknown -> no climb, and the caller is told the class is unknown."""
    esc = escalate_tier(TIER_LOW, "d-unknown", failure_class="unknown")
    assert esc.action == "no_climb"
    assert esc.tier_to is None
    assert esc.unknown_class is True


def test_unrecognized_class_fails_loud():
    """A class the table does not know raises — never a silent fallback to climb."""
    with pytest.raises(ValueError, match="unrecognized failure_class"):
        escalate_tier(TIER_LOW, "d-bogus", failure_class="something_else")


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

    esc = escalate_tier(TIER_LOW, "d-rejected", failure_class="model_error")
    assert esc.tier_from == TIER_LOW
    assert esc.tier_to == TIER_MID
    assert esc.parent_dispatch == "d-rejected"


# ---------------------------------------------------------------------------
# OI-1360: the safety net must not cost more than the escalation it is not
#
# This file's own docstring has claimed "a safety net, never an escalation" since it
# was written, and pinned only the ladder's monotonicity. Nothing measured the
# fallback chains, so the sentence described an intention.
#
# It is measurable once "escalation" is given a price: escalate_tier climbs to the
# next rung in escalation_order, so a fallback dearer than that rung is literally
# more expensive than the escalation it is not supposed to be.
#
# Measured on main 17de88de, FIVE steps breach it — including one on the kimi-k3 rung
# that OI-1360 does not mention. They are recorded below rather than silently fixed:
# repairing them means changing what production routes to when a lane goes down, and
# for tier-zero there is no compliant option at all except local_gemma (every other
# different-provider lane costs more than the ceiling by construction). That is an
# operator decision, not a mechanical edit. What this PR removes is the ability for a
# SIXTH one to appear unnoticed.
# ---------------------------------------------------------------------------

from providers.smart_router import tier_routing as _tr

#: The five breaches present on 2026-08-29, each (tier, provider, model).
#: Shrinking this set is the fix; growing it must be a deliberate, argued act.
_RECORDED_ESCALATING_FALLBACKS = {
    ("tier-zero", "kimi", "kimi-k3"),
    ("tier-zero", "codex", "gpt-5.5"),
    ("tier-low", "kimi", "kimi-k3"),
    ("tier-low", "codex", "gpt-5.5"),
    ("kimi-k3", "codex", "gpt-5.5"),
}


def _as_keys(violations) -> set:
    return {(v.tier, v.provider, v.model) for v in violations}


def test_ceiling_on_the_ladder_is_the_next_rung_up():
    """A tier that can escalate is bounded by what escalating would have cost."""
    for tier in (TIER_ZERO, TIER_LOW):
        nxt = next_tier(tier)
        assert nxt is not None
        spec = _tr._TIER_MAP[nxt]
        assert _tr.fallback_cost_ceiling(tier) == _tr._output_cost(spec.provider, spec.model)


def test_ceiling_off_the_ladder_is_the_tier_s_own_primary():
    """kimi-k3 is dispatchable and fallback-eligible but never a climb destination,
    so there is no 'next rung'. Its net may not cost more than what it replaces."""
    assert next_tier("kimi-k3") is None
    spec = _tr._TIER_MAP["kimi-k3"]
    assert _tr.fallback_cost_ceiling("kimi-k3") == _tr._output_cost(spec.provider, spec.model)


def test_the_check_discriminates(monkeypatch):
    """The guard must be able to say both yes and no — a check that cannot fail is
    not a check."""
    monkeypatch.setattr(_tr, "_output_cost", lambda _p, _m: 1.0)
    assert _tr.escalating_fallbacks() == (), "uniform prices cannot breach a ceiling"

    def _pricey(provider, model):
        return 1000.0 if provider in ("kimi", "codex") else 1.0

    monkeypatch.setattr(_tr, "_output_cost", _pricey)
    assert _tr.escalating_fallbacks(), "an expensive fallback must be reported"


def test_no_new_escalating_fallback_appeared():
    """Red the moment a sixth breach is added, or an existing one is repaired without
    recording it here."""
    live = _as_keys(_tr.escalating_fallbacks())
    added = live - _RECORDED_ESCALATING_FALLBACKS
    removed = _RECORDED_ESCALATING_FALLBACKS - live
    assert not added, (
        f"new escalating fallback(s): {sorted(added)}. A fallback dearer than one rung "
        "up is not a safety net — it is the escalation the design says it must never be."
    )
    assert not removed, (
        f"escalating fallback(s) repaired: {sorted(removed)} — good. Remove them from "
        "_RECORDED_ESCALATING_FALLBACKS in the same commit so the set keeps shrinking."
    )


def test_the_recorded_breaches_are_real_and_quantified():
    """Guards against the exemption list outliving the thing it exempts."""
    by_key = {(v.tier, v.provider, v.model): v for v in _tr.escalating_fallbacks()}
    assert set(by_key) == _RECORDED_ESCALATING_FALLBACKS
    for key, v in by_key.items():
        assert v.cost > v.ceiling, f"{key} is recorded as a breach but does not breach"
        assert v.factor > 1.0


def test_walking_to_an_escalating_fallback_is_marked_in_the_reason(monkeypatch):
    """The dispatch still proceeds — availability beats cost when the primary is down —
    but the receipt must not record a 17x price rise as an ordinary fallback."""
    monkeypatch.setattr(
        _tr, "lane_available", lambda *a, **k: (True, None), raising=False
    )

    import providers.smart_router.availability as _av

    def _only_last_available(provider, **_kw):
        return (provider == "codex", "forced unavailable")

    monkeypatch.setattr(_av, "lane_available", _only_last_available)

    route = resolve_tier_route(TIER_ZERO, {})
    assert route.provider == "codex"
    # tier-zero -> gpt-5.5 is x34.5, i.e. OVER the cap, so this must carry the
    # stronger marker. Asserting the milder one here used to pass purely because
    # the over-cap string contained it.
    assert route.reason and _tr.OVER_CAP_FALLBACK_MARKER in route.reason
    assert _tr.ESCALATING_FALLBACK_MARKER not in route.reason


def test_a_non_escalating_fallback_is_not_marked(monkeypatch):
    """The marker must mean something — it may not appear on every fallback."""
    import providers.smart_router.availability as _av

    monkeypatch.setattr(_av, "lane_available", lambda p, **k: (p == "codex", "down"))
    monkeypatch.setattr(_tr, "_output_cost", lambda _p, _m: 1.0)

    route = resolve_tier_route(TIER_ZERO, {})
    assert route.reason and _tr.ESCALATING_FALLBACK_MARKER not in route.reason


# ---------------------------------------------------------------------------
# The bounded promise (OI-1360, option B)
#
# "Never an escalation" — factor 1.0 — cannot be met by this fleet, so stating it
# is a promise that reads well and routes nothing. MAX_FALLBACK_ESCALATION_FACTOR
# replaces it with a bound that IS meetable, derived from the fleet rather than
# chosen to fit the map: 3.0 is the smallest round bound under which every rung can
# still have a different-provider net at all.
#
# The point of the bound is that it still REJECTS things. It rejects two of the five
# breaches present today, both on tier-zero, and those two stay visible as a fleet
# gap rather than dissolving into the rewritten promise.
# ---------------------------------------------------------------------------

#: Breaches ABOVE the bound: not a stretched safety net but a place where the fleet
#: has no compliant lane. Shrinking this is the fix; growing it is a regression.
_FLEET_GAP = {
    ("tier-zero", "kimi", "kimi-k3"),
    ("tier-zero", "codex", "gpt-5.5"),
}


def test_the_cap_still_rejects_something():
    """A bound that admits everything is not a bound. If this ever goes empty because
    the cap was raised rather than the map repaired, the promise was adjusted until
    it fitted — the exact move the constant exists to prevent."""
    assert _as_keys(_tr.over_cap_fallbacks()) == _FLEET_GAP


def test_the_cap_is_not_below_what_the_fleet_can_offer():
    """Derivation, pinned: below this factor tier-zero has no different-provider net
    at all except local_gemma. Recomputed from the registry, not restated."""
    spec = _tr._TIER_MAP[TIER_ZERO]
    ceiling = _tr.fallback_cost_ceiling(TIER_ZERO)
    from providers.provider_registry import load

    # dispatch_enum is None means the section is unreachable by tier routing at all
    # (_output_cost resolves by dispatch_enum), so those models cannot be anyone's
    # safety net and must not set the floor. Leaving them in understated it: glm-5.2
    # (zai, no dispatch_enum) gave 2.78 where the real floor is 2.87.
    cheapest = min(
        m.cost_output_per_mtok
        for name, cfg in load().items()
        if cfg.dispatch_enum is not None
        and cfg.dispatch_enum != spec.provider
        and name != "local_gemma"
        for m in cfg.models.values()
        if getattr(m, "dispatch_allowed", True)
    )
    required = cheapest / ceiling
    assert _tr.MAX_FALLBACK_ESCALATION_FACTOR >= required, (
        f"the cap (x{_tr.MAX_FALLBACK_ESCALATION_FACTOR}) is below the x{required:.2f} "
        "that tier-zero needs for any different-provider fallback to exist. A cap that "
        "low leaves the cheapest rung with no safety net at all."
    )


def test_the_bounded_breaches_are_actually_bounded():
    """Everything not in the fleet gap must sit at or under the cap — otherwise the
    split between 'stretched' and 'broken' is fiction."""
    for v in _tr.escalating_fallbacks():
        if (v.tier, v.provider, v.model) in _FLEET_GAP:
            continue
        assert v.factor <= _tr.MAX_FALLBACK_ESCALATION_FACTOR, f"{v} exceeds the cap"


def test_over_cap_is_a_strict_subset_of_escalating():
    over = set(_as_keys(_tr.over_cap_fallbacks()))
    allv = set(_as_keys(_tr.escalating_fallbacks()))
    assert over < allv, "the fleet gap must be a proper subset of all breaches"


def test_walking_over_the_cap_gets_the_stronger_marker(monkeypatch):
    """A x34.5 step and a x1.5 step must not read the same in a receipt."""
    import providers.smart_router.availability as _av

    monkeypatch.setattr(_av, "lane_available", lambda p, **k: (p == "codex", "down"))
    route = resolve_tier_route(TIER_ZERO, {})
    assert route.provider == "codex"
    assert _tr.OVER_CAP_FALLBACK_MARKER in route.reason
    assert "cap x3.0" in route.reason


def test_a_bounded_escalation_gets_the_milder_marker(monkeypatch):
    import providers.smart_router.availability as _av

    monkeypatch.setattr(_av, "lane_available", lambda p, **k: (p == "codex", "down"))
    route = resolve_tier_route("kimi-k3", {})
    assert route.provider == "codex"
    assert _tr.ESCALATING_FALLBACK_MARKER in route.reason
    assert _tr.OVER_CAP_FALLBACK_MARKER not in route.reason


def test_unroutable_sections_cannot_set_the_floor():
    """A section with dispatch_enum=None is unreachable by tier routing, so its prices
    must not enter the derivation. Pinned because leaving them in silently understated
    the floor (2.78 instead of 2.87) by leaning on glm-5.2, which cannot be routed to."""
    from providers.provider_registry import load

    registry = load()
    unroutable = {name for name, cfg in registry.items() if cfg.dispatch_enum is None}
    assert unroutable, "fixture expects at least one unroutable section"
    for name in sorted(unroutable):
        for model_key in registry[name].models:
            assert _tr._output_cost(None, model_key) is None, (
                f"{name}/{model_key} priced through an enum lookup with no enum — "
                "None == None would let an unroutable section answer a routing question"
            )


def test_the_two_markers_are_not_substrings_of_each_other():
    """Otherwise `MILD in reason` is true for an over-cap route and any test meant to
    tell them apart passes on both — the right answer by the wrong mechanism."""
    mild = _tr.ESCALATING_FALLBACK_MARKER
    over = _tr.OVER_CAP_FALLBACK_MARKER
    assert mild != over
    assert mild not in over, f"{over!r} contains {mild!r}"
    assert over not in mild, f"{mild!r} contains {over!r}"
