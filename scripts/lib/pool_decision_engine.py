"""pool_decision_engine.py — Pure decision functions for elastic worker pools.

No SQLite, no filesystem, no subprocess. Pure functions over PoolState +
PoolConfig + Membership list. Returns PoolDecision.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass(frozen=True)
class PoolConfig:
    pool_id: str
    min_workers: int
    max_workers: int
    scaling_policy: str          # "fixed" | "queue_aware"
    provider_mix: List[str]      # e.g. ["claude", "claude", "litellm:deepseek"]
    cooldown_seconds: float = 60.0
    heartbeat_stale_seconds: float = 300.0


@dataclass(frozen=True)
class Membership:
    membership_id: str
    terminal_id: str
    provider: str
    pool_role: str
    status: str                  # "pending" | "active" | "draining" | "reaped"
    joined_at: float             # unix timestamp
    last_heartbeat: Optional[float] = None


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
    3. Scaling policy: fixed | queue_aware
    4. Clamp delta to [min - current, max - current]
    """
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

    target = _compute_target(config, state)
    if target is None:
        return PoolDecision(
            action="noop",
            reason=f"unknown scaling policy: {config.scaling_policy}",
        )

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


def _compute_target(config: PoolConfig, state: PoolState) -> Optional[int]:
    if config.scaling_policy == "fixed":
        return config.min_workers
    if config.scaling_policy == "queue_aware":
        raw = _ceil_div(state.queue_depth, 2) if state.queue_depth > 0 else config.min_workers
        return max(config.min_workers, min(raw, config.max_workers))
    return None


def _is_stale(member: Membership, now: float, threshold: float) -> bool:
    if member.last_heartbeat is None:
        return (now - member.joined_at) > threshold
    return (now - member.last_heartbeat) > threshold


def _ceil_div(a: int, b: int) -> int:
    return math.ceil(a / b)
