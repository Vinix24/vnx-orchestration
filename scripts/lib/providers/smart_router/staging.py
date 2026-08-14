"""staging.py — Phased rollout gate for smart-router AUTO routing.

Sits UNDER the kill-switch (``VNX_SMART_ROUTER_DISABLE`` / ``VNX_AUTO_ROUTE``)
in ``door_routing.resolve_door_route``: the kill-switch always turns the router
off entirely; this layer decides, per classified cost tier, whether the router
may actually hand back a route yet. Two knobs, both operator-config
(``project_config``):

  * per-tier enable flags — ``VNX_SMART_ROUTER_TIER_{ZERO,LOW,MID,HIGH}``. A tier
    that is not enabled declines, so tier-zero (cheap deepseek) can be rolled
    out while tier-high (opus, the expensive/heavy lane) stays off.
  * a canary fraction — ``VNX_SMART_ROUTER_CANARY_PCT`` (0-100). Within an
    enabled tier only this fraction of dispatches routes; the rest follow the
    existing (legacy) path. The split is DETERMINISTIC per dispatch, not random
    per call: a dispatch that is re-evaluated must land in the same group or
    the canary cannot be compared (``claudedocs`` framework research: a canary
    without a control group measures selection bias instead of effect).

The control group (canary-out) is recognisable in the trail: the decline reason
``staging-canary-control:<tier>`` is written into the receipt, so the routed and
non-routed halves of one tier can be separated later.

Defaults are all OFF (no tier enabled, canary 0): the rollout starts from zero
and ramps up via operator-config, matching the scout pre-pass opt-in pattern.
Resolved through ``config_runtime`` so an operator's dashboard/operator-config
value is honoured — not the code default alone (the scout-prepass pitfall:
``config_runtime.get_bool()`` without the DB layer lies). Absent any value this
reads exactly the env/default, so behaviour is preserved.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .cost_tier import TIER_HIGH, TIER_LOW, TIER_MID, TIER_ZERO


# Canonical tier -> operator-config enable flag. One flag per cost tier so the
# rollout is independently toggleable (tier-zero on while tier-high off).
TIER_FLAGS: dict[str, str] = {
    TIER_ZERO: "VNX_SMART_ROUTER_TIER_ZERO",
    TIER_LOW: "VNX_SMART_ROUTER_TIER_LOW",
    TIER_MID: "VNX_SMART_ROUTER_TIER_MID",
    TIER_HIGH: "VNX_SMART_ROUTER_TIER_HIGH",
}

CANARY_PCT_KEY = "VNX_SMART_ROUTER_CANARY_PCT"

# Stable decline-reason prefixes (the tier is appended at the call site so a
# decline is diagnosable per tier in the dry-run output and receipt).
DECLINE_TIER_DISABLED = "staging-tier-disabled"
DECLINE_CANARY_CONTROL = "staging-canary-control"


@dataclass(frozen=True)
class StagingConfig:
    """Resolved staging rollout state for one process/project."""

    enabled_tiers: frozenset[str] = frozenset()
    canary_pct: int = 0


def load_staging_config() -> StagingConfig:
    """Resolve the staging rollout config through the operator-config facade.

    Reads via ``config_runtime`` so a dashboard/operator-config (project_config)
    DB value is honoured. Defaults are all OFF; an unparseable canary value
    fails closed to 0 (routes nothing, never everything).
    """
    import config_runtime  # noqa: PLC0415 — lazy; avoid import cost at module load

    enabled = frozenset(
        tier for tier, key in TIER_FLAGS.items() if config_runtime.get_bool(key)
    )
    return StagingConfig(
        enabled_tiers=enabled,
        canary_pct=_parse_pct(config_runtime.get(CANARY_PCT_KEY)),
    )


def _parse_pct(raw: Optional[str]) -> int:
    """Parse a canary percentage, failing closed: an unparseable value routes
    NOTHING (0), never everything (100)."""
    if raw is None:
        return 0
    try:
        pct = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    if pct < 0:
        return 0
    if pct > 100:
        return 100
    return pct


def dispatch_group_key(
    dispatch_id: Optional[str],
    target_slot: str,
    instruction_text: str,
    file_paths: Optional[list[str]] = None,
) -> str:
    """Stable identity string for a dispatch, used to seed the canary bucket.

    The dispatch_id is the strongest identity when available. The real door call
    site does not (yet) pass it, so the fallback is the dispatch's own content —
    target slot + instruction text + file paths — which is immutable for a given
    dispatch across re-evaluations.
    """
    parts = [
        dispatch_id or "",
        target_slot or "",
        instruction_text or "",
        "\0".join(sorted(file_paths or [])),
    ]
    return "\0".join(parts)


def canary_bucket(key: str) -> int:
    """Deterministic bucket in [0, 100) for a dispatch identity string.

    Uses SHA-256, never Python's ``hash()`` — ``hash()`` is salted per process,
    so a dispatch would fall in a different group on every run and the canary
    could not be compared across evaluations.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big")
    return value % 100


def staging_verdict(tier: str, key: str, config: StagingConfig) -> Optional[str]:
    """Decide whether a classified dispatch may route.

    Returns ``None`` (route) or a decline-reason string (follow the legacy
    path). Order: (1) the tier must be enabled, (2) the dispatch must fall
    inside the canary fraction. Either failing declines with a reason that names
    the tier, so the audit trail separates "tier disabled" from "canary control
    group".
    """
    if tier not in config.enabled_tiers:
        return f"{DECLINE_TIER_DISABLED}:{tier}"
    if config.canary_pct <= 0:
        return f"{DECLINE_CANARY_CONTROL}:{tier}"
    if config.canary_pct >= 100:
        return None
    if canary_bucket(key) < config.canary_pct:
        return None
    return f"{DECLINE_CANARY_CONTROL}:{tier}"
