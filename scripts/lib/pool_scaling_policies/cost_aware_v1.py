"""cost_aware_v1 — Queue-aware scaling with cost ceiling.

Falls back to current size if estimated hourly cost would exceed pool_config.cost_ceiling_usd.
Reads cost data from scripts/lib/cost_tracker.py (which reads from t0_receipts.ndjson per PR-7.6).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from .queue_depth_v1 import queue_depth_v1

if TYPE_CHECKING:
    from pool_decision_engine import Membership, PoolConfig, PoolState

log = logging.getLogger(__name__)


def cost_aware_v1(config: "PoolConfig", state: "PoolState", members: "List[Membership]") -> int:
    """Returns queue_depth_v1 target, clamped by cost ceiling.

    Only blocks scale-UP events. Scale-down is never blocked.
    When no ceiling is defined or cost_tracker fails, behaves identically to queue_depth_v1.
    """
    proposed_target = queue_depth_v1(config, state, members)
    active_count = len([m for m in members if m.status == "active"])

    # Scale-down never blocked by cost ceiling.
    if proposed_target <= active_count:
        return proposed_target

    ceiling = getattr(config, "cost_ceiling_usd", None)
    if ceiling is None or ceiling <= 0:
        return proposed_target

    estimated_hourly = _estimate_hourly_cost(
        members, getattr(config, "provider_mix", []), proposed_target
    )

    if estimated_hourly > ceiling:
        log.warning(
            "cost_aware_v1: estimated_hourly=%.4f > ceiling=%.4f; holding at current=%d",
            estimated_hourly,
            ceiling,
            active_count,
        )
        return active_count

    return proposed_target


def _estimate_hourly_cost(members: list, provider_mix: list, target_size: int) -> float:
    """Estimate USD/hour at given pool size.

    Uses recent receipts from cost_tracker for per-provider cost estimates.
    Falls back to $0.50/hour per worker when cost_tracker is unavailable or has no data
    (0.0 from cost_tracker means 'no receipts in window', not 'truly free').
    """
    conservative = target_size * 0.5
    try:
        from cost_tracker import recent_cost_per_hour  # type: ignore[import]
        result = recent_cost_per_hour(target_size, provider_mix or [])
        return result if result > 0.0 else conservative
    except (ImportError, Exception):
        return conservative
