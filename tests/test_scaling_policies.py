"""test_scaling_policies.py — Tests for pluggable scaling policy registry.

Covers: queue_depth_v1, fixed, cost_aware_v1 (no-receipt fallback),
POLICIES registry, backward-compat alias, and decide() integration.

Wave 6 PR-6.4 — ADR-018 pluggable scaling policies.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_decision_engine import Membership, PoolConfig, PoolState, decide  # noqa: E402
from pool_scaling_policies import POLICIES, cost_aware_v1, queue_depth_v1  # noqa: E402
from pool_scaling_policies.queue_depth_v1 import queue_depth_v1 as _q_fn  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from pool_state_fixtures import make_config, make_member, make_state  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cfg(
    min_workers: int = 1,
    max_workers: int = 4,
    policy: str = "queue_depth_v1",
    cost_ceiling: Optional[float] = None,
) -> PoolConfig:
    return PoolConfig(
        pool_id="default",
        min_workers=min_workers,
        max_workers=max_workers,
        scaling_policy=policy,
        provider_mix=["claude"],
        cooldown_seconds=0.0,
        heartbeat_stale_seconds=300.0,
        cost_ceiling_usd=cost_ceiling,
    )


def st8(queue: int = 0, now: float = 1000.0) -> PoolState:
    return make_state(queue_depth=queue, last_scaled_at=None, now=now)


def active_member(mid: str = "m-1", tid: str = "T1", joined: float = 900.0) -> Membership:
    return make_member(membership_id=mid, terminal_id=tid, status="active",
                       joined_at=joined, last_heartbeat=990.0)


def n_active(n: int) -> List[Membership]:
    return [active_member(mid=f"m-{i}", tid=f"T{i}", joined=900.0 + i)
            for i in range(n)]


# ---------------------------------------------------------------------------
# Table-driven: pure policy function return values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy,queue,current_n,min_w,max_w,expected_target", [
    # queue_depth_v1
    ("queue_depth_v1", 0,   1, 1, 4, 1),   # queue=0 → ceil(0/2)=0 → max(min,0)=1
    ("queue_depth_v1", 2,   1, 1, 4, 1),   # ceil(2/2)=1 → target=1
    ("queue_depth_v1", 3,   1, 1, 4, 2),   # ceil(3/2)=2
    ("queue_depth_v1", 4,   1, 1, 4, 2),   # ceil(4/2)=2
    ("queue_depth_v1", 5,   1, 1, 4, 3),   # ceil(5/2)=3
    ("queue_depth_v1", 100, 1, 1, 4, 4),   # capped at max=4
    ("queue_depth_v1", 0,   0, 2, 6, 2),   # queue=0 below min=2 → target=min=2
    ("queue_depth_v1", 1,   0, 2, 6, 2),   # ceil(1/2)=1 < min=2 → target=2
    # fixed
    ("fixed",          0,   1, 1, 4, 1),   # always min
    ("fixed",          100, 0, 2, 6, 2),   # always min regardless of queue
    ("fixed",          50,  4, 3, 8, 3),   # always min=3
    # queue_aware (alias for queue_depth_v1)
    ("queue_aware",    4,   0, 1, 4, 2),   # alias works → ceil(4/2)=2
    ("queue_aware",    0,   1, 1, 4, 1),   # alias at zero queue
    # cost_aware_v1 — no receipt file, falls back to queue_depth_v1 result
    ("cost_aware_v1",  4,   0, 1, 4, 2),
    ("cost_aware_v1",  0,   1, 1, 4, 1),
])
def test_policy_returns_expected_target(
    policy, queue, current_n, min_w, max_w, expected_target
):
    config = cfg(min_workers=min_w, max_workers=max_w, policy=policy)
    state = st8(queue=queue)
    members = n_active(current_n)
    policy_fn = POLICIES[policy]
    result = policy_fn(config, state, members)
    assert result == expected_target, (
        f"policy={policy} queue={queue} current={current_n} "
        f"expected={expected_target} got={result}"
    )


# ---------------------------------------------------------------------------
# POLICIES registry integrity
# ---------------------------------------------------------------------------

def test_policies_registry_contains_all_required_keys():
    assert "fixed" in POLICIES
    assert "queue_depth_v1" in POLICIES
    assert "queue_aware" in POLICIES
    assert "cost_aware_v1" in POLICIES


def test_queue_aware_alias_is_same_function_as_queue_depth_v1():
    assert POLICIES["queue_aware"] is POLICIES["queue_depth_v1"]


def test_policies_are_callable():
    for name, fn in POLICIES.items():
        assert callable(fn), f"POLICIES[{name!r}] is not callable"


# ---------------------------------------------------------------------------
# decide() integration: policy routing via registry
# ---------------------------------------------------------------------------

def test_decide_with_queue_depth_v1_policy():
    config = cfg(policy="queue_depth_v1", min_workers=1, max_workers=4)
    state = st8(queue=4)
    result = decide(config, state, [])
    assert result.action == "scale_up"
    assert result.delta == 2  # ceil(4/2)=2, current=0


def test_decide_with_queue_aware_alias():
    config = cfg(policy="queue_aware", min_workers=1, max_workers=4)
    state = st8(queue=4)
    result = decide(config, state, [])
    assert result.action == "scale_up"
    assert result.delta == 2


def test_decide_with_fixed_policy():
    config = cfg(policy="fixed", min_workers=2, max_workers=6)
    state = st8(queue=100)
    result = decide(config, state, [])
    assert result.action == "scale_up"
    assert result.delta == 2  # scale up to min=2 from 0


def test_decide_with_fixed_policy_at_min_is_noop():
    config = cfg(policy="fixed", min_workers=1, max_workers=4)
    state = st8(queue=100)
    members = [active_member()]
    result = decide(config, state, members)
    assert result.action == "noop"


def test_decide_with_cost_aware_v1_no_ceiling():
    config = cfg(policy="cost_aware_v1", min_workers=1, max_workers=4)
    state = st8(queue=4)
    result = decide(config, state, [])
    assert result.action == "scale_up"
    assert result.delta == 2  # falls back to queue_depth_v1


def test_decide_unknown_policy_returns_noop():
    config = cfg(policy="nonexistent_xyz")
    state = st8(queue=10)
    result = decide(config, state, [])
    assert result.action == "noop"
    assert "unknown" in result.reason.lower()


# ---------------------------------------------------------------------------
# queue_depth_v1 direct edge cases
# ---------------------------------------------------------------------------

def test_queue_depth_v1_queue_zero_returns_min():
    config = cfg(min_workers=3, max_workers=8, policy="queue_depth_v1")
    state = st8(queue=0)
    result = _q_fn(config, state, [])
    assert result == 3


def test_queue_depth_v1_queue_one_rounds_up():
    config = cfg(min_workers=1, max_workers=4, policy="queue_depth_v1")
    state = st8(queue=1)
    result = _q_fn(config, state, [])
    assert result == 1  # ceil(1/2)=1 = min


def test_queue_depth_v1_respects_max_workers():
    config = cfg(min_workers=1, max_workers=2, policy="queue_depth_v1")
    state = st8(queue=100)
    result = _q_fn(config, state, [])
    assert result == 2


def test_queue_depth_v1_ignores_member_count_for_target():
    config = cfg(min_workers=1, max_workers=8, policy="queue_depth_v1")
    state = st8(queue=6)
    members_0 = []
    members_3 = n_active(3)
    # Target is state-based, not current-size-based
    assert _q_fn(config, state, members_0) == _q_fn(config, state, members_3) == 3


# ---------------------------------------------------------------------------
# Backward-compat: PR-6.3 queue_aware still produces identical results
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("queue,min_w,max_w", [
    (0,   1, 4),
    (2,   1, 4),
    (5,   1, 4),
    (100, 1, 4),
    (3,   2, 6),
])
def test_queue_aware_and_queue_depth_v1_identical(queue, min_w, max_w):
    state = st8(queue=queue)
    members = []
    cfg_qa = cfg(policy="queue_aware",    min_workers=min_w, max_workers=max_w)
    cfg_qd = cfg(policy="queue_depth_v1", min_workers=min_w, max_workers=max_w)
    assert POLICIES["queue_aware"](cfg_qa, state, members) == \
           POLICIES["queue_depth_v1"](cfg_qd, state, members)
