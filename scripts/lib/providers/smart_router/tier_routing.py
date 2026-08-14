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
branch. The chain terminates in an ungated lane (claude/codex), so a route is
always returned.

Constraint references (provider_constraints.yaml):
  kimi-via-cli-only: kimi_cli lane, never via=api/moonshot
  deepseek-harness-subscription-blocked: DEEPSEEK_API_KEY required; own key only
  zai-via-openrouter-only / deprecated-glm-models: not routed by this static map
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from providers.provider_registry import RegistryLookupError, TierRouteSpec, load_tier_map

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


def _walk_chain(
    route: TierRoute,
    env: dict,
    state_dir: Optional[Path],
    now: Optional[float],
) -> TierRoute:
    """Walk the fallback chain, skipping unavailable lanes (OI-1185).

    Missing key, CLI absent, and cooldown all funnel through the same
    ``lane_available`` gate, so they walk the same chain. The terminal lane is
    ungated (claude/codex), so this always returns a route.
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
                    current, reason=_fallback_reason(current, skipped),
                )
            return current
        skipped.append(f"{current.provider} unavailable ({reason})")
        current = current.fallback
    # Unreachable in practice: every chain terminates in an ungated lane
    # (claude/codex). Defensive only — the door must never receive None.
    return last


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
