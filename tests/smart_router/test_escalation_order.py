"""Red tests for the escalation-order / cost-ladder separation (OI-1356).

The operator decided (19-08) on option (a): the cost ladder
(``routing.tier_map`` in wave7_models.yaml) stays purely cost-ordered, and
quality escalation gets its OWN, explicitly authored list --
``routing.escalation_order`` -- a subset/reordering of the tier_map keys that
describes the climb chain:

    tier-zero -> tier-low -> tier-mid -> tier-high -> fable-5

``kimi-k3`` and ``gpt-5.5`` stay full tier_map rungs (dispatch/fallback still
reach them) but are NOT climb destinations: ``next_tier()`` must return
``None`` for them.

Today neither ``escalation_order`` nor its consumption in
``tier_routing.next_tier()`` exists -- ``next_tier()`` still walks
``_TIER_LADDER`` (the cost order). This file pins the CONTRACT a fix must
satisfy; it does not implement it (no production code or yaml is touched
here).

Two design choices this file makes, since they are not specified verbatim by
the dispatch instruction and a test must commit to *something* runnable:

* Exception class: ``RegistryLookupError`` (already defined in
  provider_registry.py for exactly this class of error -- "the registry does
  not know X", used by ``_resolve_step``/``load_tier_map``/
  ``load_tier_ladder`` today) is asserted for the unknown-member validation.

* Testing seam for "next_tier() must read a DIFFERENT registry": tier_routing
  resolves ``_TIER_MAP``/``_TIER_LADDER`` exactly ONCE, at module import, by
  calling the provider_registry loaders with no explicit ``registry_path`` --
  which fall back to ``provider_registry._REGISTRY_PATH`` (see tier_routing.py
  lines 73-82, "resolved once at import... loud and early", ADR-036). The
  same must be true of whatever resolves the escalation order, or a malformed
  ``escalation_order`` would only surface mid-dispatch instead of at import.
  So the fixture-registry tests below patch ``provider_registry._REGISTRY_PATH``
  and exec a throwaway, unregistered COPY of tier_routing.py against it --
  never ``importlib.reload`` the real, shared module. tier_routing.py defines
  ``TierRoute``/``TierEscalation`` as classes; reloading it in place rebinds
  those names to NEW class objects, and every other test file that already
  holds a reference to the pre-reload class (e.g. test_tier_routing.py's
  ``from providers.smart_router.tier_routing import TierRoute`` for its own
  ``isinstance`` checks) then breaks for the rest of the pytest session --
  confirmed empirically while writing this file: an in-place reload-then-
  restore still left ``TierRoute`` a *third*, different object, and broke
  ``test_route_dispatch_default_on``/``test_route_dispatch_auto_route_enabled``
  in test_tier_routing.py even though every VALUE had been restored. A
  throwaway copy, executed under its own private module name, leaves the
  real module -- and its classes' identity -- untouched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers import provider_registry
from providers.provider_registry import RegistryLookupError, load_tier_ladder
from providers.smart_router.cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO
from providers.smart_router.tier_routing import escalate_tier, next_tier

_TIER_ROUTING_PATH = Path(provider_registry.__file__).parent / "smart_router" / "tier_routing.py"


# ---------------------------------------------------------------------------
# Fixture-registry helpers (self-contained -- do not import from
# test_cost_ladder.py, which is independently red right now for unrelated
# stale-price reasons and is explicitly out of scope for this dispatch).
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


def _write_registry(
    tmp_path: Path,
    providers: dict,
    tier_map: dict,
    escalation_order: list | None = None,
) -> Path:
    routing: dict = {"tier_map": tier_map}
    if escalation_order is not None:
        routing["escalation_order"] = escalation_order
    path = tmp_path / "wave7_models.yaml"
    # sort_keys=False: dict order IS the authored order under test, same
    # reasoning as test_cost_ladder.py's own _write_registry.
    path.write_text(
        yaml.dump({"providers": providers, "routing": routing}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _load_fresh_tier_routing():
    """Exec an independent copy of tier_routing.py under a private module name.

    Registered in sys.modules only for the duration of exec_module (dataclass
    field resolution needs ``sys.modules[cls.__module__]`` to exist for the
    ``Optional["TierRoute"]`` forward reference on the frozen dataclass), then
    removed immediately -- this throwaway copy is never a real, importable
    module and must not linger in the module cache.
    """
    spec = importlib.util.spec_from_file_location(
        "providers.smart_router._test_oi1356_tier_routing_fixture", _TIER_ROUTING_PATH
    )
    module = importlib.util.module_from_spec(spec)  # __package__ derives from the dotted name
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


@pytest.fixture
def reload_tier_routing(monkeypatch):
    """Load a throwaway tier_routing copy against a fixture registry path.

    Patches provider_registry._REGISTRY_PATH (auto-reverted by monkeypatch at
    teardown) so the fresh copy's module-level ``_TIER_MAP = load_tier_map()``
    etc. resolve against the fixture instead of the real wave7_models.yaml.
    The real, shared providers.smart_router.tier_routing module is never
    touched -- see _load_fresh_tier_routing's docstring for why that matters.
    """

    def _reload(fixture_path: Path):
        monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", fixture_path)
        return _load_fresh_tier_routing()

    return _reload


# ---------------------------------------------------------------------------
# Rood vandaag: next_tier() must read escalation_order, not the cost ladder
# ---------------------------------------------------------------------------

def test_next_tier_tier_mid_climbs_to_tier_high_not_kimi_k3():
    """escalation_order: tier-mid -> tier-high. Today next_tier reads the cost
    ladder, where kimi-k3 sits between tier-mid and tier-high -- RED today."""
    assert next_tier(TIER_MID) == TIER_HIGH


def test_next_tier_tier_low_climbs_to_tier_mid():
    """escalation_order: tier-low -> tier-mid (never kimi-k3, never gpt-5.5)."""
    assert next_tier(TIER_LOW) == TIER_MID
    assert next_tier(TIER_LOW) != "kimi-k3"
    assert next_tier(TIER_LOW) != "gpt-5.5"


def test_next_tier_kimi_k3_is_not_an_escalation_destination():
    """kimi-k3 stays a full tier_map rung but is excluded from
    escalation_order -- no climb. Today it returns tier-high -- RED today."""
    assert next_tier("kimi-k3") is None


def test_next_tier_gpt_5_5_is_not_an_escalation_destination():
    """gpt-5.5 stays a full tier_map rung but is excluded from
    escalation_order -- no climb. Today it returns fable-5 -- RED today."""
    assert next_tier("gpt-5.5") is None


def test_next_tier_fable_5_is_top_of_chain():
    """fable-5 is the top of escalation_order -- no climb beyond it."""
    assert next_tier("fable-5") is None


def test_next_tier_tier_high_climbs_to_fable_5_not_gpt_5_5():
    """escalation_order: tier-high -> fable-5. Today next_tier reads the cost
    ladder, where gpt-5.5 sits between tier-high and fable-5 -- RED today."""
    assert next_tier(TIER_HIGH) == "fable-5"


def test_next_tier_full_escalation_chain():
    """The complete climb chain the operator authored, end to end."""
    assert next_tier(TIER_ZERO) == TIER_LOW
    assert next_tier(TIER_LOW) == TIER_MID
    assert next_tier(TIER_MID) == TIER_HIGH
    assert next_tier(TIER_HIGH) == "fable-5"
    assert next_tier("fable-5") is None


def test_escalate_tier_mid_to_tier_high_via_escalation_order():
    """A REJECTED result at tier-mid climbs to tier-high (escalation_order),
    not kimi-k3 (cost ladder) -- RED today."""
    esc = escalate_tier(TIER_MID, "d-esc-mid", failure_class="model_error")
    assert esc.tier_from == TIER_MID
    assert esc.tier_to == TIER_HIGH


# ---------------------------------------------------------------------------
# Registry-loader validation: an escalation_order member absent from
# tier_map must fail loud, not silently drop the rung.
# ---------------------------------------------------------------------------

def test_escalation_order_unknown_member_fails_loud(tmp_path, reload_tier_routing):
    """Today nothing parses routing.escalation_order at all, so this fixture
    is silently ignored and no exception is raised -- reload succeeds and
    pytest.raises reports "DID NOT RAISE": a clear, readable signal that the
    validation behavior does not exist yet (not an accidental KeyError deep
    in a helper)."""
    providers = {"pa": _provider("pa", {"cheap": _model(1.0)})}
    tier_map = {"x": {"provider": "pa", "model": "cheap", "lane": "l"}}
    path = _write_registry(
        tmp_path, providers, tier_map, escalation_order=["x", "unknown-tier"]
    )

    with pytest.raises(RegistryLookupError) as exc_info:
        reload_tier_routing(path)
    assert "unknown-tier" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Groen vandaag, moet groen blijven: the separation must not shrink or
# weaken the cost ladder, and escalate_tier's action table is untouched.
# ---------------------------------------------------------------------------

def test_load_tier_ladder_still_returns_all_seven_rungs_in_cost_order():
    """Separating escalation_order from tier_map must not shrink the cost
    ladder -- kimi-k3 and gpt-5.5 stay full rungs, still strictly cost-ordered."""
    ladder = load_tier_ladder()
    tiers = [rung.tier for rung in ladder]
    assert tiers == [
        TIER_ZERO, TIER_LOW, TIER_MID, "kimi-k3", TIER_HIGH, "gpt-5.5", "fable-5",
    ]
    costs = [rung.output_cost_per_mtok for rung in ladder]
    assert all(a < b for a, b in zip(costs, costs[1:])), costs


def test_load_tier_ladder_still_rejects_non_monotonic_cost(tmp_path):
    """Anti-overshoot: load_tier_ladder's strict-cost invariant is unrelated
    to escalation_order and must keep failing loud on its own terms. Repeated
    here self-contained (not imported from test_cost_ladder.py) so this
    file's own green baseline never depends on a neighbor file that is
    independently red right now for unrelated stale-price reasons."""
    providers = {
        "pa": _provider("pa", {"cheap": _model(5.0)}),
        "pb": _provider("pb", {"pricey": _model(1.0)}),  # cheaper -- non-monotonic
    }
    tier_map = {
        "tier-zero": {"provider": "pa", "model": "cheap", "lane": "l"},
        "tier-low": {"provider": "pb", "model": "pricey", "lane": "l"},
    }
    path = _write_registry(tmp_path, providers, tier_map)
    with pytest.raises(RegistryLookupError, match="strict cost ladder"):
        load_tier_ladder(registry_path=path)


def test_escalate_tier_action_table_unaffected_by_source_change():
    """escalate_tier must keep driving its ACTION off _ESCALATION_TABLE
    (model_error/auth_rejected/timeout/...); only the climb DESTINATION
    changes source, from the cost ladder to escalation_order. Uses tier-mid,
    which stays in escalation_order, so a None destination can never mask an
    action-logic regression."""
    climb = escalate_tier(TIER_MID, "d-action-climb", failure_class="model_error")
    assert climb.action == "climb"
    assert climb.tier_to == TIER_HIGH  # RED today: destination is still kimi-k3

    no_climb = escalate_tier(TIER_MID, "d-action-auth", failure_class="auth_rejected")
    assert no_climb.action == "no_climb"
    assert no_climb.tier_to is None

    retry = escalate_tier(TIER_MID, "d-action-timeout", failure_class="timeout")
    assert retry.action == "retry_same_tier"
    assert retry.tier_to == TIER_MID

    climbed_after_retry = escalate_tier(
        TIER_MID, "d-action-timeout", failure_class="timeout", retried=True
    )
    assert climbed_after_retry.action == "climb"
    assert climbed_after_retry.tier_to == TIER_HIGH  # RED today: still kimi-k3


# ---------------------------------------------------------------------------
# The test that guards the separation itself (the core of OI-1356): a
# fixture where cost order and escalation_order deliberately diverge.
# ---------------------------------------------------------------------------

def test_next_tier_follows_escalation_order_not_cost_order_when_they_diverge(
    tmp_path, reload_tier_routing
):
    """Cost order (cheapest first): a (1) < b (2) < c (3) -- a cost-ladder
    walk goes a -> b -> c. escalation_order is authored as a -> c -> b: a
    DIFFERENT order over the same three tiers. If next_tier() is ever
    reverted to read the cost ladder instead of escalation_order, this test
    fails. A test that stays green under that revert is worthless (per the
    dispatch instruction) -- this is that test."""
    providers = {
        "pa": _provider("pa", {"cheap": _model(1.0)}),
        "pb": _provider("pb", {"mid": _model(2.0)}),
        "pc": _provider("pc", {"pricey": _model(3.0)}),
    }
    tier_map = {
        "a": {"provider": "pa", "model": "cheap", "lane": "l"},
        "b": {"provider": "pb", "model": "mid", "lane": "l"},
        "c": {"provider": "pc", "model": "pricey", "lane": "l"},
    }
    path = _write_registry(tmp_path, providers, tier_map, escalation_order=["a", "c", "b"])
    reloaded = reload_tier_routing(path)

    assert reloaded.next_tier("a") == "c", "must follow escalation_order (a->c), not cost order (a->b)"
    assert reloaded.next_tier("c") == "b"
    assert reloaded.next_tier("b") is None  # top of escalation_order, despite being cheaper than c
