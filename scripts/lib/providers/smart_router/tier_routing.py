"""tier_routing.py — Map cost tiers to provider/lane routing specs.

Tier→provider mappings (honoring provider_constraints.yaml):
  tier-zero → local Gemma e4b via MLX; fallback Ollama
  tier-low  → DeepSeek via Claude-harness key-auth (DEEPSEEK_API_KEY required)
               OR Kimi via CLI (kimi-via-cli-only constraint)
  tier-mid  → claude-sonnet-4-6
  tier-high → claude-opus-4-8

Constraint references (provider_constraints.yaml):
  kimi-via-cli-only: Kimi must use lane='kimi_cli', never via=api/moonshot
  deepseek-harness-subscription-blocked: DEEPSEEK_API_KEY required; subscription-
    redirect blocked. Allowed only with via=claude_harness_keyed + own key.
  zai-via-openrouter-only: GLM only via OpenRouter (no direct Zhipu API)
  deprecated-glm-models: GLM-4.5/4.6 blocked; use glm-5.1 via OpenRouter
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


_ROUTE_ZERO_FALLBACK = TierRoute(
    tier=TIER_ZERO,
    provider="ollama",
    model="gemma:4b",
    lane="ollama",
)

_ROUTE_ZERO = TierRoute(
    tier=TIER_ZERO,
    provider="local-gemma",
    model="gemma-4b-e4b-mlx",
    lane="mlx",
    fallback=_ROUTE_ZERO_FALLBACK,
)

_ROUTE_KIMI = TierRoute(
    tier=TIER_LOW,
    provider="kimi",
    model="kimi-k2",
    lane="kimi_cli",  # kimi-via-cli-only: never via=api or moonshot
)

_ROUTE_MID = TierRoute(
    tier=TIER_MID,
    provider="claude",
    model="claude-sonnet-4-6",
    lane="tmux_interactive",
)

_ROUTE_HIGH = TierRoute(
    tier=TIER_HIGH,
    provider="claude",
    model="claude-opus-4-8",
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

    For tier-low: prefers DeepSeek claude-harness (key-auth) when DEEPSEEK_API_KEY
    is present, falls back to Kimi CLI. Unknown tier strings default to tier-high.
    """
    _env = env if env is not None else dict(os.environ)

    if tier == TIER_ZERO:
        return _ROUTE_ZERO

    if tier == TIER_LOW:
        if _deepseek_available(_env):
            return TierRoute(
                tier=tier,
                provider="deepseek",
                model="deepseek-chat",
                lane="claude_harness_keyed",
                env_requirements=("DEEPSEEK_API_KEY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
                fallback=_ROUTE_KIMI,
            )
        return _ROUTE_KIMI

    if tier == TIER_MID:
        return _ROUTE_MID

    return _ROUTE_HIGH
