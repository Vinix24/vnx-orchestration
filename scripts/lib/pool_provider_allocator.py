"""pool_provider_allocator.py — Provider-mix allocation for elastic pools.

Pure function (no I/O). Given current pool members + provider_mix + target_delta,
returns ordered list of providers for new workers.

Algorithm: lowest-share-first (highest gap from target wins each slot).
For provider_mix=["claude","claude","codex"], target=4, current=[]:
  Allocations:
  - claude (target=2, current=0, gap=2) -> 1st spawn
  - claude (target=2, current=1, gap=1) -> 2nd spawn
  - codex (target=1, current=0, gap=1) -> 3rd spawn (tie resolved by dict order)
  - claude or codex (both at target) -> 4th spawn falls back to fallback_provider

Provider-binding is immutable per worker lifetime — this module only
decides providers for NEW workers; existing members are never reassigned.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from pool_decision_engine import Membership  # noqa: E402


@dataclass(frozen=True)
class AllocationResult:
    providers: List[str]       # ordered list, len == delta for scale_up
    fallback_used: List[str]   # providers chosen via fallback when mix saturated


def compute_target_shares(provider_mix: List[str], pool_size: int) -> Dict[str, int]:
    """For a pool of given size, what is the target count per provider?

    Args:
        provider_mix: e.g. ["claude", "claude", "codex"] — defines proportions
        pool_size: total workers in pool

    Returns:
        {provider: count} summing to pool_size
    """
    if not provider_mix:
        return {}
    mix_size = len(provider_mix)
    mix_counter = Counter(provider_mix)
    target: Dict[str, int] = {}
    remaining = pool_size
    sorted_providers = sorted(mix_counter.items(), key=lambda x: -x[1])

    for i, (provider, mix_count) in enumerate(sorted_providers):
        if i == len(sorted_providers) - 1:
            target[provider] = remaining
        else:
            count = int(round(pool_size * mix_count / mix_size))
            target[provider] = count
            remaining -= count

    total = sum(target.values())
    if total != pool_size:
        diff = pool_size - total
        first = sorted_providers[0][0]
        target[first] = target[first] + diff

    return target


def allocate_for_scale_up(
    members: List[Membership],
    provider_mix: List[str],
    delta: int,
    fallback_provider: str = "claude",
) -> AllocationResult:
    """Decide which providers to spawn for scale-up.

    Strategy: pick providers with the largest gap (target - current).
    Provider-binding is immutable — existing members keep their provider.

    Args:
        members: current pool members (provider from .provider, status from .status)
        provider_mix: pool configuration list
        delta: number of new workers to spawn (positive)
        fallback_provider: used when mix is empty or all targets saturated

    Returns:
        AllocationResult with ordered providers list (len == delta).
    """
    if delta <= 0:
        return AllocationResult(providers=[], fallback_used=[])

    if not provider_mix:
        return AllocationResult(
            providers=[fallback_provider] * delta,
            fallback_used=[fallback_provider] * delta,
        )

    new_size = len(members) + delta
    target_shares = compute_target_shares(provider_mix, new_size)
    current_shares = Counter(m.provider for m in members if m.status == "active")

    allocations: List[str] = []
    fallback_used: List[str] = []
    pending: Dict[str, int] = dict(current_shares)

    for _ in range(delta):
        gaps = {p: target_shares.get(p, 0) - pending.get(p, 0) for p in target_shares}
        positive = {p: g for p, g in gaps.items() if g > 0}

        if positive:
            chosen = max(positive.items(), key=lambda x: (x[1], -ord(x[0][0])))[0]
        else:
            chosen = fallback_provider
            fallback_used.append(chosen)

        allocations.append(chosen)
        pending[chosen] = pending.get(chosen, 0) + 1

    return AllocationResult(providers=allocations, fallback_used=fallback_used)


def select_for_scale_down(
    members: List[Membership],
    provider_mix: List[str],
    delta: int,
) -> List[str]:
    """Return membership_ids to release on scale-down.

    Strategy: release oldest worker of the provider with the highest excess
    over its target share. Falls back to overall oldest when all providers
    are at or below target.

    Args:
        members: current pool members
        provider_mix: pool configuration list
        delta: negative int (number of workers to remove)

    Returns:
        List of membership_ids to reap, len == abs(delta).
    """
    if delta >= 0:
        return []

    active = [m for m in members if m.status == "active"]
    new_size = len(active) + delta
    target_shares = compute_target_shares(provider_mix, max(new_size, 0)) if provider_mix else {}
    current_shares = Counter(m.provider for m in active)
    excess = {p: current_shares.get(p, 0) - target_shares.get(p, 0) for p in current_shares}

    candidates: List[Membership] = []
    while len(candidates) < abs(delta):
        positive_excess = {p: e for p, e in excess.items() if e > 0}
        chosen_provider: Optional[str] = None
        if positive_excess:
            chosen_provider = max(positive_excess.items(), key=lambda x: x[1])[0]

        pool = (
            [m for m in active if m.provider == chosen_provider]
            if chosen_provider
            else list(active)
        )
        pool = [m for m in pool if m not in candidates]
        if not pool:
            break
        oldest = min(pool, key=lambda m: m.joined_at)
        candidates.append(oldest)
        if chosen_provider:
            excess[chosen_provider] = excess[chosen_provider] - 1

    return [m.membership_id for m in candidates]
