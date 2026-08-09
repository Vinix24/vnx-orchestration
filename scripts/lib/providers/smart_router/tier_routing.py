"""tier_routing.py — Map cost tiers to provider/lane routing specs.

Tier→provider mappings (honoring provider_constraints.yaml):
  tier-zero → DeepSeek (deepseek-v4-flash) via Claude-harness key-auth
               (DEEPSEEK_API_KEY required); fallback Codex via provider lane.
               Local-gemma route preserved but unused — reactivate when
               gemma-4-12b-integration ships.
  tier-low  → DeepSeek (deepseek-v4-flash) via Claude-harness key-auth
               (DEEPSEEK_API_KEY required); fallback Codex via provider lane
               (kimi quota exhausted 2026-08-02, OI-940)
  tier-mid  → sonnet-5  (canonical registry key — see wave7_models.yaml)
  tier-high → opus-5    (canonical registry key — see wave7_models.yaml)

Model names here are canonical registry keys from wave7_models.yaml, never
free-form strings: the registry is the single source of truth for model
identity (dispatch-20260802-model-ssot-en-ketenlink). The previous 4-series
ids (claude-sonnet-4-6 / claude-opus-4-8) were not registry keys and would
reject with model-not-in-current-registry the moment tier-mid/tier-high
actually routed; the fleet now runs the 5-series.

Constraint references (provider_constraints.yaml):
  kimi-via-cli-only: Kimi must use lane='kimi_cli', never via=api/moonshot
  deepseek-harness-subscription-blocked: DEEPSEEK_API_KEY required; subscription-
    redirect blocked. Allowed only with via=claude_harness_keyed + own key.
  zai-via-openrouter-only: GLM only via OpenRouter (no direct Zhipu API)
  deprecated-glm-models: GLM-4.5/4.6/5/5.1 blocked; use glm-5.2 via OpenRouter
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .cost_tier import TIER_ZERO, TIER_LOW, TIER_MID, TIER_HIGH


@dataclass(frozen=True)
class TierRoute:
    """Provider routing spec for a cost tier."""

    tier: str
    provider: str
    model: str
    lane: str
    env_requirements: tuple = field(default_factory=tuple)
    fallback: Optional["TierRoute"] = None


# ── tier-zero / tier-low routes ──
# DeepSeek flash via claude-harness is the primary route for both entry tiers
# (operator decision 2026-08-02: skip local models for now).  When
# DEEPSEEK_API_KEY is absent, both fall back to Codex via provider lane
# (kimi quota exhausted 2026-08-02, OI-940).  resolve_tier_route() creates
# routes on the fly so the tier field is correct in every return value.

# Preserved but unused: local Gemma route. Reactivate when gemma-4-12b-integration
# ships (queued track). The Gemma chain (primary + Ollama fallback) is intact below
# but never returned by resolve_tier_route.
# _ROUTE_LOCAL_GEMMA_FALLBACK = TierRoute(
#     tier=TIER_ZERO,
#     provider="ollama",
#     model="gemma:4b",
#     lane="ollama",
# )
# _ROUTE_LOCAL_GEMMA = TierRoute(
#     tier=TIER_ZERO,
#     provider="local-gemma",
#     model="gemma-4b-e4b-mlx",
#     lane="mlx",
#     fallback=_ROUTE_LOCAL_GEMMA_FALLBACK,
# )

# Preserved but unused: Kimi CLI route (kimi-via-cli-only constraint).  Kimi quota
# was exhausted 2026-08-02 (OI-940); Codex is the active fallback.  Reactivate when
# quota is restored or a new Kimi model tier is added.
# _ROUTE_KIMI = TierRoute(
#     tier=TIER_LOW,
#     provider="kimi",
#     model="kimi-k2",
#     lane="kimi_cli",
# )

_ROUTE_MID = TierRoute(
    tier=TIER_MID,
    provider="claude",
    model="sonnet-5",  # canonical registry key (wave7_models.yaml)
    lane="tmux_interactive",
)

_ROUTE_HIGH = TierRoute(
    tier=TIER_HIGH,
    provider="claude",
    model="opus-5",  # canonical registry key (wave7_models.yaml)
    lane="tmux_interactive",
)


def _deepseek_available(env: dict) -> bool:
    """DeepSeek harness is allowed only when DEEPSEEK_API_KEY is present.

    Implements deepseek-harness-subscription-blocked: own key + hardening required;
    routing through the production OAuth subscription is blocked.
    """
    return bool(env.get("DEEPSEEK_API_KEY"))


def resolve_tier_route(tier: str, env: Optional[dict] = None) -> TierRoute:
    """Resolve a cost tier to a TierRoute.

    For tier-zero and tier-low: prefers DeepSeek claude-harness (key-auth) when
    DEEPSEEK_API_KEY is present; falls back to Codex via provider lane (kimi quota
    exhausted 2026-08-02, OI-940; local models skipped until gemma-4-12b-integration
    ships). Unknown tier strings default to tier-high.
    """
    _env = env if env is not None else dict(os.environ)

    if tier in (TIER_ZERO, TIER_LOW):
        if _deepseek_available(_env):
            return TierRoute(
                tier=tier,
                provider="deepseek",
                model="deepseek-v4-flash",  # deepseek-chat discontinued 2026-07-24
                lane="claude_harness_keyed",
                env_requirements=("DEEPSEEK_API_KEY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
                fallback=TierRoute(
                    tier=tier,
                    provider="codex",
                    model="gpt-5.5",
                    lane="provider",
                ),
            )
        return TierRoute(
            tier=tier,
            provider="codex",
            model="gpt-5.5",
            lane="provider",
        )

    if tier == TIER_MID:
        return _ROUTE_MID

    return _ROUTE_HIGH
