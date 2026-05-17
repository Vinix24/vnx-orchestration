"""pool_decision_engine.py — Pure decision functions for elastic worker pools.

No SQLite, no filesystem, no subprocess. Pure functions over PoolState +
PoolConfig + Membership list. Returns PoolDecision.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
Wave 6 PR-6.4 — Pluggable policy registry via pool_scaling_policies.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

# Ensure pool_scaling_policies sub-package is importable.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


POOL_HEARTBEAT_STALE_SECONDS: float = 180.0


@dataclass(frozen=True)
class PoolConfig:
    pool_id: str
    min_workers: int
    max_workers: int
    scaling_policy: str          # "fixed" | "queue_aware" | "queue_depth_v1" | "cost_aware_v1"
    provider_mix: List[str]      # e.g. ["claude", "claude", "litellm:deepseek"]
    cooldown_seconds: float = 60.0
    heartbeat_stale_seconds: float = POOL_HEARTBEAT_STALE_SECONDS
    cost_ceiling_usd: Optional[float] = None  # used by cost_aware_v1


@dataclass(frozen=True)
class Membership:
    membership_id: str
    terminal_id: str
    provider: str
    pool_role: str
    status: str                  # "pending" | "active" | "draining" | "reaped"
    joined_at: float             # unix timestamp
    last_heartbeat: Optional[float] = None
    pid: Optional[int] = None


@dataclass(frozen=True)
class PoolState:
    queue_depth: int             # pending dispatches
    last_scaled_at: Optional[float]  # cooldown anchor
    now: float                   # current time for testability


@dataclass(frozen=True)
class PoolDecision:
    action: Literal["noop", "scale_up", "scale_down", "reap"]
    delta: int = 0
    reason: str = ""
    targets: List[str] = field(default_factory=list)
    cooldown_remaining_s: float = 0.0


def decide(
    config: PoolConfig,
    state: PoolState,
    members: List[Membership],
) -> PoolDecision:
    """Pure decision: given current state, what should the pool do?

    Evaluation order:
    1. Stale heartbeats -> reap with targets
    2. Cooldown active -> noop with cooldown_remaining
    3. Policy registry lookup (fixed | queue_aware | queue_depth_v1 | cost_aware_v1)
    4. Compute delta and return scale_up / scale_down / noop
    """
    from pool_scaling_policies import POLICIES

    active = [m for m in members if m.status == "active"]
    current = len(active)

    stale = [m for m in active if _is_stale(m, state.now, config.heartbeat_stale_seconds)]
    if stale:
        return PoolDecision(
            action="reap",
            targets=[m.membership_id for m in stale],
            reason=(
                f"{len(stale)} workers heartbeat-stale "
                f"(>{config.heartbeat_stale_seconds}s)"
            ),
        )

    if state.last_scaled_at is not None:
        elapsed = state.now - state.last_scaled_at
        if elapsed < config.cooldown_seconds:
            remaining = config.cooldown_seconds - elapsed
            return PoolDecision(
                action="noop",
                reason=f"cooldown active ({elapsed:.1f}s / {config.cooldown_seconds}s)",
                cooldown_remaining_s=remaining,
            )

    policy_fn = POLICIES.get(config.scaling_policy)
    if policy_fn is None:
        return PoolDecision(
            action="noop",
            reason=f"unknown policy: {config.scaling_policy}",
        )

    target = policy_fn(config, state, members)

    delta = target - current
    if delta > 0:
        return PoolDecision(
            action="scale_up",
            delta=delta,
            reason=f"target={target} from queue_depth={state.queue_depth}",
        )
    if delta < 0:
        sorted_active = sorted(active, key=lambda m: m.joined_at)
        targets = [m.membership_id for m in sorted_active[: abs(delta)]]
        return PoolDecision(
            action="scale_down",
            delta=delta,
            reason=f"target={target} current={current}",
            targets=targets,
        )
    return PoolDecision(action="noop", reason="at target")


def _is_stale(member: Membership, now: float, threshold: float) -> bool:
    if member.last_heartbeat is None:
        return (now - member.joined_at) > threshold
    return (now - member.last_heartbeat) > threshold


def _ceil_div(a: int, b: int) -> int:
    return math.ceil(a / b)
