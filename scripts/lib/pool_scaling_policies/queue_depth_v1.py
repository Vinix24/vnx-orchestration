"""queue_depth_v1 — Scale based on pending dispatches.

target = clamp(min_workers, ceil(queue_depth / 2), max_workers)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from pool_decision_engine import Membership, PoolConfig, PoolState


def queue_depth_v1(config: "PoolConfig", state: "PoolState", members: "List[Membership]") -> int:
    """Returns target pool size based on queue depth.

    Args:
        config: PoolConfig (min_workers, max_workers, etc)
        state: PoolState (queue_depth, now, etc)
        members: List[Membership] (active members)

    Returns:
        Target pool size (int).
    """
    raw_target = -(-state.queue_depth // 2)  # ceil(queue/2) without math import
    target = max(config.min_workers, raw_target)
    target = min(target, config.max_workers)
    return target
