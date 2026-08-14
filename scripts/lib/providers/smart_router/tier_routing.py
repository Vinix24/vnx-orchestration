"""tier_routing.py — Map cost tiers to provider/lane routing specs.

Tier→provider mappings (honoring provider_constraints.yaml):
  tier-zero → DeepSeek (deepseek-v4-flash) via Claude-harness key-auth
               (DEEPSEEK_API_KEY required); fallback Codex via provider lane.
  tier-low  → DeepSeek (deepseek-v4-flash) via Claude-harness key-auth
               (DEEPSEEK_API_KEY required); runtime fallback Kimi CLI
               (kimi-k3, kimi-via-cli-only) then Codex.
  tier-mid  → sonnet-5  (canonical registry key — see wave7_models.yaml)
  tier-high → opus-5    (canonical registry key — see wave7_models.yaml)

Model names here are canonical registry keys from wave7_models.yaml, never
free-form strings: the registry is the single source of truth for model
identity (dispatch-20260802-model-ssot-en-ketenlink). The previous 4-series
ids (claude-sonnet-4-6 / claude-opus-4-8) were not registry keys and would
reject with model-not-in-current-registry the moment tier-mid/tier-high
actually routed; the fleet now runs the 5-series.

Availability is a runtime signal, not a code comment. Each non-Claude lane is
gated by ``availability.lane_available`` (env vars, CLI presence, cooldown) at
decision time. A lane that fails on quota/auth is recorded by
``availability.record_lane_failure`` and auto-recovers after its cooldown
period — no code edit or release required to bring it back. Kimi is a regular
route in the tier map again (through the availability check), and local-gemma
stays disabled via the availability layer's explicit reason, not as a
commented-out block.

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
from pathlib import Path
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
    reason: Optional[str] = None  # why this route was chosen over its primary


def _codex_route(tier: str, reason: Optional[str] = None) -> TierRoute:
    """Codex vangnet — the last-resort provider lane (OI-940)."""
    return TierRoute(
        tier=tier,
        provider="codex",
        model="gpt-5.5",
        lane="provider",
        reason=reason,
    )


def _deepseek_route(tier: str) -> TierRoute:
    """DeepSeek flash via claude-harness key-auth (deepseek-chat discontinued
    2026-07-24)."""
    return TierRoute(
        tier=tier,
        provider="deepseek",
        model="deepseek-v4-flash",
        lane="claude_harness_keyed",
        env_requirements=("DEEPSEEK_API_KEY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
        fallback=_codex_route(tier),
    )


def _kimi_route(tier: str, reason: Optional[str] = None) -> TierRoute:
    """Kimi CLI route (kimi-via-cli-only: never via=api/moonshot).

    kimi-k3 is the canonical registry key (kimi_cli.default_model in
    wave7_models.yaml). Codex remains the vangnet behind it.
    """
    return TierRoute(
        tier=tier,
        provider="kimi",
        model="kimi-k3",
        lane="kimi_cli",
        fallback=_codex_route(tier),
        reason=reason,
    )


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


def _deepseek_has_key(env: dict) -> bool:
    """True when DEEPSEEK_API_KEY is present (static check, no cooldown).

    Kept separate from the full availability check so the missing-key case
    preserves the pre-existing behaviour (codex fallback, kimi NOT consulted)
    while the key-present-but-in-cooldown case can fall through to kimi.
    Implements deepseek-harness-subscription-blocked: own key + hardening
    required; routing through the production OAuth subscription is blocked.
    """
    return bool(env.get("DEEPSEEK_API_KEY"))


def resolve_tier_route(
    tier: str,
    env: Optional[dict] = None,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> TierRoute:
    """Resolve a cost tier to a TierRoute.

    Entry tiers (zero/low) prefer DeepSeek claude-harness when DEEPSEEK_API_KEY
    is present; a missing key falls back to Codex (existing behaviour). When
    DeepSeek has a key but is in cooldown after a quota/auth failure, the router
    falls back to Kimi CLI (kimi-via-cli-only) and then Codex — availability is
    read at decision time so a restored lane rejoins without a release. Unknown
    tier strings default to tier-high.
    """
    _env = env if env is not None else dict(os.environ)

    if tier in (TIER_ZERO, TIER_LOW):
        return _resolve_entry_tier_route(tier, _env, state_dir, now)

    if tier == TIER_MID:
        return _ROUTE_MID

    return _ROUTE_HIGH


def _resolve_entry_tier_route(
    tier: str,
    env: dict,
    state_dir: Optional[Path],
    now: Optional[float],
) -> TierRoute:
    from .availability import lane_available  # noqa: PLC0415

    deepseek_ok, deepseek_reason = lane_available(
        "deepseek", env=env, state_dir=state_dir, now=now,
    )
    if deepseek_ok:
        return _deepseek_route(tier)

    if _deepseek_has_key(env):
        # Key present but DeepSeek is in cooldown → try Kimi before the vangnet.
        kimi_ok, kimi_reason = lane_available(
            "kimi", env=env, state_dir=state_dir, now=now,
        )
        if kimi_ok:
            return _kimi_route(
                tier,
                reason=f"deepseek unavailable ({deepseek_reason}); using kimi",
            )
        return _codex_route(
            tier,
            reason=(
                f"deepseek unavailable ({deepseek_reason}); "
                f"kimi unavailable ({kimi_reason}); using codex"
            ),
        )

    # Missing key → existing behaviour: Codex vangnet (kimi not consulted).
    return _codex_route(tier)
