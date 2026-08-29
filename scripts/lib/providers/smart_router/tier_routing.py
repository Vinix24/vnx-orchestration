"""tier_routing.py — Map cost tiers to provider/lane routing specs.

Model identity and provider strings are read from wave7_models.yaml (ADR-036):
there are zero model names and zero provider strings as Python literals on this
routing path. ``provider_registry.load_tier_map()`` resolves the registry's
``routing.tier_map`` block — a provider key to its dispatch enum, a model key to
a validated registry model — and raises RegistryLookupError on anything the
registry does not know. A malformed registry therefore fails at import, before
any dispatch reaches the lookup (loud and early).

Availability is a runtime signal, not a code comment. Each lane is gated at
decision time by ``availability.lane_available`` (env vars, CLI presence,
cooldown). A lane that is unavailable — missing key, CLI absent, or in cooldown
after a quota/auth failure — is skipped and the next step in the ``fallback``
chain takes over (OI-1185). Missing key and cooldown follow the SAME chain: the
availability layer is the single gate, so there is no separate "missing key"
branch. The chain terminates in codex (or claude, for tiers that route there
directly): a lane with no env/CLI requirement of its own, but NOT exempt from
the cooldown gate (OI-1330) — ``record_lane_failure`` has a real production
caller now (``provider_dispatch._maybe_record_provider_lane_cooldown``), and a
codex quota failure is daily practice. So the terminal can itself be
unavailable, with nowhere further to walk. ``_walk_chain`` still always
returns a route (the door must never receive None) but never silently: when
the terminal itself was unavailable, the returned route carries the
accumulated skipped-reason instead of ``reason=None``.

Two mechanisms live here and are deliberately opposite in intent (OI-1221):

  * AVAILABILITY fallback — ``resolve_tier_route`` + ``_walk_chain``. An
    unavailable lane is skipped and the next chain step takes over ON THE SAME
    TIER. A safety net, never an escalation.
  * QUALITY escalation — ``escalate_tier`` + ``next_tier``. A REJECTED result
    fires a followup dispatch one tier UP the cost ladder (tier_to =
    tier_from + 1), linked by parent_dispatch. It climbs; it never walks the
    fallback list.

Constraint references (provider_constraints.yaml):
  kimi-via-cli-only: kimi_cli lane, never via=api/moonshot
  deepseek-harness-subscription-blocked: DEEPSEEK_API_KEY required; own key only
  zai-via-openrouter-only / deprecated-glm-models: not routed by this static map

Vocabulary note (P3, dispatch 20260821-q3-failure-class-split): ``escalate_tier``
only ever receives the lowercase ``failure_classification.FAILURE_CLASSES``
vocabulary (its sole production caller, ``dispatch_cli._maybe_stage_escalation``,
derives ``failure_class`` from ``classify_failure_safe``). ``exit_classifier.py``'s
separate UPPERCASE ``FC_*`` taxonomy (headless CLI runs) never reaches this
function — see ``failure_classification.py``'s module docstring for the full
traced call chain, and ``tests/test_smart_router_quality_tier.py`` for the test
that pins the separation.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from providers.provider_registry import (
    RegistryLookupError,
    TierRouteSpec,
    load_escalation_order,
    load_tier_ladder,
    load_tier_map,
)

from .cost_tier import TIER_HIGH


@dataclass(frozen=True)
class TierRoute:
    """Provider routing spec for a cost tier."""

    tier: str
    provider: str
    model: str
    lane: str
    env_requirements: tuple = field(default_factory=tuple)
    fallback: Optional["TierRoute"] = None
    reason: Optional[str] = None  # why this route was chosen over its primary


# Resolved once at import (ADR-036): a malformed registry raises
# RegistryLookupError here, before any dispatch reaches the lookup — loud and
# early rather than discovered mid-dispatch.
_TIER_MAP = load_tier_map()

# The cost-ordered ladder (cheapest first), derived from the registry's own
# prices. load_tier_ladder() fails loud on a duplicate rung or a non-monotonic
# price order — same fail-early contract as _TIER_MAP above. Used by
# load_tier_ladder's own callers (cost reporting, classification); NOT read by
# next_tier()/escalate_tier() below — see _ESCALATION_ORDER (OI-1356).
_TIER_LADDER = load_tier_ladder()

# The quality-escalation climb order (OI-1356): a SEPARATE, explicitly
# authored list from _TIER_LADDER above. tier_map/_TIER_LADDER answer "what
# does this tier cost"; escalation_order answers "where does a REJECTED
# result climb to". Resolved once at import, same fail-early contract as
# _TIER_MAP/_TIER_LADDER: a registry missing routing.escalation_order, or
# listing a member that is not a tier_map key, raises RegistryLookupError
# here — before any dispatch reaches next_tier()/escalate_tier().
_ESCALATION_ORDER = load_escalation_order()


# The escalation decision table (dispatch 20260816-p6-escalate-tier-ds,
# extended P3 dispatch 20260821-q3-failure-class-split): the closed set of
# failure classes and the action each maps to. A class the table does not
# know fails loudly — never a silent fallback to "climb".
#   model_error      -> climb one tier
#   credit_exhausted -> climb one tier AND notify the operator
#   auth_rejected    -> no climb (a higher tier has the same auth problem)
#   timeout          -> retry the same tier first, then climb
#   empty_completion -> retry the same tier first, then climb
#   completion_without_execution -> climb one tier (a higher-tier model can
#                       actually execute the tool calls it fabricated instead
#                       of claiming completion without running them)
#   no_verdict       -> retry the same tier first, then climb (a gate-runner
#                       process flake — e.g. codex never fed stdin and
#                       produced no verdict event — not a real model/provider
#                       error, so it is a flake exactly like timeout)
#   tool_missing     -> no climb (a missing CLI binary on this host is not
#                       fixed by picking a different model on the same host)
#   unknown          -> no climb, report the unknown class loudly (the
#                       vangnetklasse — kept as the catch-all; do not remove)
_ESCALATION_TABLE = {
    "model_error": "climb",
    "credit_exhausted": "climb",
    "auth_rejected": "no_climb",
    "timeout": "retry_same_tier",
    "empty_completion": "retry_same_tier",
    "completion_without_execution": "climb",
    "no_verdict": "retry_same_tier",
    "tool_missing": "no_climb",
    "unknown": "no_climb",
}

# Failure classes that must ALSO reach the operator, independent of whether a
# climb is possible (credit_exhausted is actionable even at the top rung).
_NOTIFY_OPERATOR_CLASSES = frozenset({"credit_exhausted"})


@dataclass(frozen=True)
class TierEscalation:
    """Quality-escalation decision for a REJECTED result (OI-1221, extended
    dispatch 20260816-p6-escalate-tier-ds).

    The decision is driven by ``failure_class`` (from ``classify_failure``, the
    real reason the attempt failed), never a bare "it failed" boolean. ``action``
    is one of:

      * ``"climb"`` — fire a followup one tier UP the cost ladder (``tier_to`` is
        the rung above ``tier_from``). ``tier_to`` is None only when the ladder
        is already topped; a caller must then stop climbing, not wrap.
      * ``"retry_same_tier"`` — fire a followup on the SAME tier
        (``tier_to`` == ``tier_from``); timeout/empty_completion retry once
        before climbing.
      * ``"no_climb"`` — do not fire any followup (auth_rejected, unknown).

    ``notify_operator`` is True when the failure class demands an operator
    notification (credit_exhausted). ``unknown_class`` is True when the class is
    ``unknown`` — the caller must report it loudly, never absorb it.
    """

    tier_from: str
    tier_to: Optional[str]
    parent_dispatch: str
    failure_class: str
    action: str
    notify_operator: bool = False
    unknown_class: bool = False


def next_tier(tier: str) -> Optional[str]:
    """Return the tier one rung up the quality-escalation climb, or None at the top.

    Reads ``_ESCALATION_ORDER`` (registry ``routing.escalation_order``,
    OI-1356) — a separate list from the cost ladder (``_TIER_LADDER``): a tier
    absent from escalation_order (kimi-k3, gpt-5.5) is still a full tier_map
    rung — dispatchable, fallback-eligible — but never a climb destination.
    An unknown tier returns None (fail-safe: do not escalate from a rung
    escalation_order does not know).
    """
    try:
        idx = _ESCALATION_ORDER.index(tier)
    except ValueError:
        return None
    return _ESCALATION_ORDER[idx + 1] if idx + 1 < len(_ESCALATION_ORDER) else None


def escalate_tier(
    tier_from: str,
    parent_dispatch: str,
    *,
    failure_class: str,
    retried: bool = False,
) -> TierEscalation:
    """Decide the escalation for a rejected result from its failure class.

    The decision table lives in code (``_ESCALATION_TABLE``), not a docstring:
    a class the table does not know raises ValueError — the caller must extend
    the table explicitly, never silently fall back to a climb. ``retried``
    encodes "this attempt is already a same-tier retry": timeout/empty_completion
    retry the same tier once, then climb on the next rejection. Quality
    escalation never walks the tier's ``fallback`` chain (availability fallback
    does that, on the same tier) — a climb moves exactly one rung up.
    """
    if failure_class not in _ESCALATION_TABLE:
        raise ValueError(
            f"unrecognized failure_class {failure_class!r}; escalation table "
            f"knows {sorted(_ESCALATION_TABLE)} — refusing to guess a climb"
        )
    action = _ESCALATION_TABLE[failure_class]
    if action == "retry_same_tier" and retried:
        action = "climb"

    if action == "climb":
        tier_to = next_tier(tier_from)
    elif action == "retry_same_tier":
        tier_to = tier_from
    else:  # no_climb
        tier_to = None

    return TierEscalation(
        tier_from=tier_from,
        tier_to=tier_to,
        parent_dispatch=parent_dispatch,
        failure_class=failure_class,
        action=action,
        notify_operator=(failure_class in _NOTIFY_OPERATOR_CLASSES),
        unknown_class=(failure_class == "unknown"),
    )



# ---------------------------------------------------------------------------
# "A safety net, never an escalation" — with a price attached (OI-1360)
#
# The sentence above appears twice: in this module's docstring and in
# wave7_models.yaml:453. Until now nothing measured it, so it described an
# intention rather than a property. It is enforceable once "escalation" is given a
# price, and it has one: escalate_tier climbs to the next rung in escalation_order.
# A fallback that costs MORE than that rung is, literally, dearer than the
# escalation it is not supposed to be.
# ---------------------------------------------------------------------------


def _output_cost(provider_enum: str, model_key: str) -> Optional[float]:
    """Output $/Mtok for a (dispatch enum, model key) pair, or None when unknown.

    ``provider`` on a tier route is the dispatch enum ("claude"), not the registry
    section key ("anthropic") — walk the sections by dispatch_enum, the same way
    provider_registry._output_cost_for does for the primary.
    """
    from providers.provider_registry import load  # noqa: PLC0415

    for cfg in load().values():
        if cfg.dispatch_enum == provider_enum:
            entry = cfg.models.get(model_key)
            if entry is not None:
                return entry.cost_output_per_mtok
    return None


def fallback_cost_ceiling(tier: str) -> Optional[float]:
    """The most a fallback on ``tier`` may cost before it stops being a safety net.

    For a tier ON the escalation ladder, the ceiling is the next rung up: that is
    exactly what escalating would have cost, so anything dearer makes falling back
    the more expensive of the two.

    For a tier OUTSIDE escalation_order (kimi-k3, gpt-5.5 — dispatchable rungs that
    are never climb destinations) there is no next rung, so the ceiling is the tier's
    own primary: a safety net may not cost more than the thing it stands in for.

    Returns None when the ceiling cannot be resolved (top of the ladder with no
    primary price) — an unknown ceiling constrains nothing and must not be treated
    as zero.
    """
    nxt = next_tier(tier)
    if nxt is not None:
        spec = _TIER_MAP.get(nxt)
        return _output_cost(spec.provider, spec.model) if spec else None
    spec = _TIER_MAP.get(tier)
    return _output_cost(spec.provider, spec.model) if spec else None


@dataclass(frozen=True)
class EscalatingFallback:
    """A fallback step that costs more than the escalation it must not be."""

    tier: str
    provider: str
    model: str
    cost: float
    ceiling: float

    @property
    def factor(self) -> float:
        return self.cost / self.ceiling if self.ceiling else float("inf")

    def __str__(self) -> str:  # pragma: no cover — formatting only
        return (
            f"{self.tier} -> {self.provider}/{self.model} at {self.cost:.2f}/Mtok, "
            f"ceiling {self.ceiling:.2f} (x{self.factor:.1f})"
        )


def escalating_fallbacks(tier_map: Optional[dict] = None) -> tuple:
    """Every fallback step in the map that breaches its tier's cost ceiling.

    Empty means the promise holds. Anything in it is a step where an availability
    fallback silently costs more than a quality escalation would.
    """
    tmap = _TIER_MAP if tier_map is None else tier_map
    out = []
    for tier, spec in tmap.items():
        ceiling = fallback_cost_ceiling(tier)
        if ceiling is None:
            continue
        for step in spec.fallback:
            cost = _output_cost(step.provider, step.model)
            if cost is not None and cost > ceiling:
                out.append(
                    EscalatingFallback(
                        tier=tier, provider=step.provider, model=step.model,
                        cost=cost, ceiling=ceiling,
                    )
                )
    return tuple(out)


def over_cap_fallbacks(tier_map: Optional[dict] = None) -> tuple:
    """Breaches above MAX_FALLBACK_ESCALATION_FACTOR — the fleet gap.

    Distinct from ``escalating_fallbacks``: that one reports every step dearer than
    its ceiling (the raw measurement). This one reports the subset the bounded
    promise does NOT cover — where the fleet has no compliant lane and the design is
    knowingly broken rather than merely stretched.
    """
    return tuple(
        v
        for v in escalating_fallbacks(tier_map)
        if v.factor > MAX_FALLBACK_ESCALATION_FACTOR
    )


#: The most a fallback may exceed its ceiling and still count as a safety net.
#:
#: The literal promise — never dearer than escalating, i.e. factor 1.0 — cannot be
#: met by this fleet, and stating it anyway is a promise that reads well and routes
#: nothing. Measured 2026-08-29: tier-zero's ceiling is 0.87/Mtok, and the cheapest
#: lane that is BOTH a different provider (a same-provider net does not survive the
#: outage it exists for) AND dispatchable is glm-5.2 at 2.42 — factor 2.78. Below
#: that, the cheapest rung has no net at all except local_gemma, a 4B local model
#: scoring 0.40 on complex_reasoning.
#:
#: So 3.0 is the smallest round bound under which every rung can still HAVE a
#: different-provider net. Derived from the fleet, not chosen to fit the map: it
#: still rejects two of the five breaches present today, both on tier-zero. Raising
#: it to admit those two is exactly the move this constant exists to prevent —
#: adjusting the promise until the map passes.
#:
#: tier-low -> gpt-5.5 sits at precisely 3.0, ON the bound rather than under it.
#: That is fragile by construction: a one-cent gpt-5.5 rise makes it a gap.
MAX_FALLBACK_ESCALATION_FACTOR = 3.0

#: Marker appended to a route reason when the lane actually walked to costs more
#: than its ceiling. The dispatch is NOT blocked — availability still wins over cost
#: when the primary is down — but the receipt says so, instead of recording a 17x
#: price rise as an ordinary fallback.
ESCALATING_FALLBACK_MARKER = "escalating-fallback"

#: Stronger marker for a step above MAX_FALLBACK_ESCALATION_FACTOR: not a bounded
#: escalation but a fleet gap, walked because nothing compliant exists.
OVER_CAP_FALLBACK_MARKER = "escalating-fallback-over-cap"

def _route_from_spec(
    spec: TierRouteSpec,
    fallback: Optional[TierRoute],
    reason: Optional[str] = None,
) -> TierRoute:
    from .availability import lane_env_vars  # noqa: PLC0415

    return TierRoute(
        tier=spec.tier,
        provider=spec.provider,
        model=spec.model,
        lane=spec.lane,
        env_requirements=lane_env_vars(spec.provider),
        fallback=fallback,
        reason=reason,
    )


def _chain_from_spec(spec: TierRouteSpec) -> TierRoute:
    """Build the primary + fallback linked chain for a tier spec."""
    fallback: Optional[TierRoute] = None
    for step in reversed(spec.fallback):
        step_spec = TierRouteSpec(
            tier=spec.tier,
            provider=step.provider,
            model=step.model,
            lane=step.lane,
            fallback=(),
        )
        fallback = _route_from_spec(step_spec, fallback)
    return _route_from_spec(spec, fallback)


def _fallback_reason(route: TierRoute, skipped: list[str]) -> str:
    return f"{'; '.join(skipped)}; using {route.provider}"


def _annotated_fallback_reason(route: TierRoute, skipped: list[str]) -> str:
    """The fallback reason, plus a marker when the lane walked to is an escalation.

    The dispatch still proceeds: when the primary lane is down, getting the work done
    beats getting it done cheaply. But a fallback that costs 53x the primary is not
    the safety net the design promises, and the receipt should not record it as an
    ordinary one. Fail-open — a price the registry cannot resolve annotates nothing.
    """
    base = _fallback_reason(route, skipped)
    try:
        ceiling = fallback_cost_ceiling(route.tier)
        cost = _output_cost(route.provider, route.model)
        if ceiling is not None and cost is not None and cost > ceiling:
            factor = cost / ceiling if ceiling else float("inf")
            marker = (
                OVER_CAP_FALLBACK_MARKER
                if factor > MAX_FALLBACK_ESCALATION_FACTOR
                else ESCALATING_FALLBACK_MARKER
            )
            return (
                f"{base}; {marker}: {cost:.2f}/Mtok vs ceiling {ceiling:.2f} "
                f"(x{factor:.1f}, cap x{MAX_FALLBACK_ESCALATION_FACTOR:.1f})"
            )
    except Exception:  # noqa: BLE001 — annotation must never break routing
        pass
    return base


def _walk_chain(
    route: TierRoute,
    env: dict,
    state_dir: Optional[Path],
    now: Optional[float],
) -> TierRoute:
    """Walk the fallback chain, skipping unavailable lanes (OI-1185).

    Missing key, CLI absent, and cooldown all funnel through the same
    ``lane_available`` gate, so they walk the same chain. This always returns
    a route — but "returned" no longer implies "available" (OI-1330): the
    terminal lane (claude/codex) has no env/CLI requirement of its own, yet it
    is still cooldown-gated like every other lane, and ``record_lane_failure``
    now has a real production caller that writes to it. When the terminal is
    ALSO unavailable there is nowhere further to walk to, so it is returned
    anyway (the door must never receive None) — but never silently: the
    accumulated skipped-reason is attached instead of dropped, so a
    receipt/report built from ``route.reason`` shows the chosen lane was
    known-dead at decision time.
    """
    from .availability import lane_available  # noqa: PLC0415

    current: Optional[TierRoute] = route
    last = route
    skipped: list[str] = []
    while current is not None:
        last = current
        ok, reason = lane_available(
            current.provider, env=env, state_dir=state_dir, now=now,
        )
        if ok:
            if skipped:
                return dataclasses.replace(
                    current, reason=_annotated_fallback_reason(current, skipped),
                )
            return current
        skipped.append(f"{current.provider} unavailable ({reason})")
        current = current.fallback
    # Every lane in the chain, including the terminal, was unavailable
    # (OI-1330). There is no further step to walk to, so the terminal is
    # still returned (the door must never receive None) — but with the
    # accumulated skipped-reason attached, never as a silent reason=None that
    # would read as a clean, available route.
    return dataclasses.replace(last, reason=_annotated_fallback_reason(last, skipped))


def resolve_tier_route(
    tier: str,
    env: Optional[dict] = None,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> TierRoute:
    """Resolve a cost tier to a TierRoute, walking the fallback chain.

    Entry tiers (zero/low) prefer DeepSeek claude-harness; an unavailable lane
    (missing DEEPSEEK_API_KEY, CLI absent, or cooldown) is skipped and the next
    chain step takes over (Kimi CLI, then Codex). Availability is read at
    decision time so a restored lane rejoins without a release. Unknown tier
    strings default to tier-high (safe over silent skip).
    """
    _env = env if env is not None else dict(os.environ)

    spec = _TIER_MAP.get(tier) or _TIER_MAP.get(TIER_HIGH)
    if spec is None:
        raise RegistryLookupError(
            f"tier {tier!r} (and the tier-high default) is missing from "
            "routing.tier_map in the registry"
        )
    return _walk_chain(_chain_from_spec(spec), _env, state_dir, now)
