"""test_pool_decision_engine.py — Unit tests for pool_decision_engine.py.

Pure tests: no SQLite, no filesystem, no subprocess.
Table-driven tests over 8 state combinations plus Hypothesis property test.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_decision_engine import (  # noqa: E402
    Membership,
    PoolConfig,
    PoolDecision,
    PoolState,
    _ceil_div,
    _is_stale,
    decide,
)

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from pool_state_fixtures import make_config, make_member, make_state  # noqa: E402

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def cfg(
    min_workers: int = 1,
    max_workers: int = 4,
    policy: str = "queue_aware",
    cooldown: float = 60.0,
    stale: float = 300.0,
) -> PoolConfig:
    return make_config(
        min_workers=min_workers,
        max_workers=max_workers,
        scaling_policy=policy,
        cooldown_seconds=cooldown,
        heartbeat_stale_seconds=stale,
    )


def st8(
    queue: int = 0,
    last_scaled: float = None,
    now: float = 1000.0,
) -> PoolState:
    return make_state(queue_depth=queue, last_scaled_at=last_scaled, now=now)


def active(
    mid: str = "m-1",
    tid: str = "T1",
    joined: float = 900.0,
    heartbeat: float = 990.0,
) -> Membership:
    return make_member(
        membership_id=mid,
        terminal_id=tid,
        status="active",
        joined_at=joined,
        last_heartbeat=heartbeat,
    )


# ---------------------------------------------------------------------------
# Table-driven tests — 8 state combinations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "scenario,config,state,members,expected_action,expected_delta",
    [
        # 1. Empty pool with queue=4 → ceil(4/2)=2 → scale_up +2
        (
            "empty_with_queue",
            cfg(min_workers=1, max_workers=4, policy="queue_aware"),
            st8(queue=4, now=1000.0),
            [],
            "scale_up",
            2,
        ),
        # 2. Full pool at max with large queue → noop (already at max)
        (
            "full_at_max",
            cfg(min_workers=1, max_workers=2, policy="queue_aware"),
            st8(queue=10, now=1000.0),
            [active("m-1", "T1"), active("m-2", "T2")],
            "noop",
            0,
        ),
        # 3. Pool below min (0 workers), queue=0 → scale_up to min
        (
            "below_min_no_queue",
            cfg(min_workers=2, max_workers=4, policy="queue_aware"),
            st8(queue=0, now=1000.0),
            [],
            "scale_up",
            2,
        ),
        # 4. Stale member present → reap before scaling
        (
            "stale_member_reaped_first",
            cfg(min_workers=1, max_workers=4, policy="queue_aware", stale=300.0),
            st8(queue=4, now=1000.0),
            [
                make_member("m-stale", "T1", joined_at=100.0, last_heartbeat=100.0, status="active"),
                active("m-fresh", "T2", joined=900.0, heartbeat=990.0),
            ],
            "reap",
            0,
        ),
        # 5. Cooldown active → noop with cooldown_remaining
        (
            "cooldown_active",
            cfg(min_workers=1, max_workers=4, policy="queue_aware", cooldown=120.0),
            st8(queue=4, last_scaled=950.0, now=1000.0),  # 50s elapsed, cooldown=120s
            [],
            "noop",
            0,
        ),
        # 6. Fixed policy ignores queue → only scale to min
        (
            "fixed_policy_ignores_queue",
            cfg(min_workers=1, max_workers=4, policy="fixed"),
            st8(queue=10, now=1000.0),
            [],
            "scale_up",
            1,  # scale_up to min=1
        ),
        # 7. Fixed policy at min → noop
        (
            "fixed_policy_at_min",
            cfg(min_workers=1, max_workers=4, policy="fixed"),
            st8(queue=10, now=1000.0),
            [active("m-1", "T1")],
            "noop",
            0,
        ),
        # 8. queue_depth=0, current=min → noop
        (
            "at_min_no_queue",
            cfg(min_workers=1, max_workers=4, policy="queue_aware"),
            st8(queue=0, now=1000.0),
            [active("m-1", "T1")],
            "noop",
            0,
        ),
    ],
)
def test_decide_returns_expected(
    scenario,
    config,
    state,
    members,
    expected_action,
    expected_delta,
):
    result = decide(config, state, members)
    assert result.action == expected_action, (
        f"[{scenario}] expected action={expected_action!r} got {result.action!r}"
    )
    if expected_delta != 0:
        assert result.delta == expected_delta, (
            f"[{scenario}] expected delta={expected_delta} got {result.delta}"
        )


# ---------------------------------------------------------------------------
# Cooldown detail tests
# ---------------------------------------------------------------------------

def test_cooldown_returns_remaining_time():
    config = cfg(cooldown=120.0)
    state = st8(queue=4, last_scaled=900.0, now=1000.0)  # 100s elapsed
    result = decide(config, state, [])
    assert result.action == "noop"
    assert abs(result.cooldown_remaining_s - 20.0) < 0.01


def test_cooldown_expired_allows_scale_up():
    config = cfg(cooldown=60.0)
    state = st8(queue=4, last_scaled=930.0, now=1000.0)  # 70s elapsed
    result = decide(config, state, [])
    assert result.action == "scale_up"


def test_no_last_scaled_skips_cooldown():
    config = cfg(cooldown=120.0)
    state = st8(queue=4, last_scaled=None, now=1000.0)
    result = decide(config, state, [])
    assert result.action == "scale_up"


# ---------------------------------------------------------------------------
# Reap detail tests
# ---------------------------------------------------------------------------

def test_reap_targets_stale_members_only():
    config = cfg(stale=300.0)
    state = st8(queue=0, now=1000.0)
    members = [
        make_member("m-stale", "T1", joined_at=100.0, last_heartbeat=100.0, status="active"),
        active("m-fresh", "T2", joined=900.0, heartbeat=990.0),
    ]
    result = decide(config, state, members)
    assert result.action == "reap"
    assert result.targets == ["m-stale"]


def test_reap_member_with_no_heartbeat_uses_joined_at():
    config = cfg(stale=300.0)
    state = st8(queue=0, now=1000.0)
    members = [
        make_member("m-no-hb", "T1", joined_at=100.0, last_heartbeat=None, status="active"),
    ]
    result = decide(config, state, members)
    assert result.action == "reap"
    assert "m-no-hb" in result.targets


def test_fresh_member_no_heartbeat_not_reaped():
    config = cfg(stale=300.0)
    state = st8(queue=0, now=1000.0)
    members = [
        make_member("m-new", "T1", joined_at=800.0, last_heartbeat=None, status="active"),
    ]
    result = decide(config, state, members)
    assert result.action != "reap"


# ---------------------------------------------------------------------------
# Scale-down detail tests
# ---------------------------------------------------------------------------

def test_scale_down_targets_oldest_first():
    config = cfg(min_workers=1, max_workers=4, policy="queue_aware")
    state = st8(queue=0, now=1000.0)  # target = min=1, current=3 → scale_down 2
    members = [
        active("m-old", "T1", joined=700.0, heartbeat=990.0),
        active("m-mid", "T2", joined=800.0, heartbeat=990.0),
        active("m-new", "T3", joined=900.0, heartbeat=990.0),
    ]
    result = decide(config, state, members)
    assert result.action == "scale_down"
    assert result.delta == -2
    # oldest two should be targeted
    assert "m-old" in result.targets
    assert "m-mid" in result.targets
    assert "m-new" not in result.targets


def test_scale_down_reason_contains_target():
    config = cfg(min_workers=1, max_workers=4, policy="queue_aware")
    state = st8(queue=0, now=1000.0)
    members = [active("m-1", "T1"), active("m-2", "T2")]
    result = decide(config, state, members)
    assert result.action == "scale_down"
    assert "target=" in result.reason


# ---------------------------------------------------------------------------
# Queue-aware scaling calculations
# ---------------------------------------------------------------------------

def test_queue_aware_ceil_div():
    config = cfg(min_workers=1, max_workers=8, policy="queue_aware")
    state = st8(queue=5, now=1000.0)
    result = decide(config, state, [])
    # ceil(5/2) = 3 → scale_up +3
    assert result.action == "scale_up"
    assert result.delta == 3


def test_queue_aware_clamped_to_max():
    config = cfg(min_workers=1, max_workers=3, policy="queue_aware")
    state = st8(queue=100, now=1000.0)
    result = decide(config, state, [])
    assert result.action == "scale_up"
    assert result.delta == 3  # target=3 (max), current=0


def test_queue_aware_clamped_to_min_when_queue_zero():
    config = cfg(min_workers=2, max_workers=6, policy="queue_aware")
    state = st8(queue=0, now=1000.0)
    result = decide(config, state, [])
    # target = min=2 (queue=0 uses min_workers), current=0 → scale_up +2
    assert result.action == "scale_up"
    assert result.delta == 2


# ---------------------------------------------------------------------------
# Unknown policy
# ---------------------------------------------------------------------------

def test_unknown_policy_returns_noop():
    config = cfg(policy="nonexistent_policy_xyz")
    state = st8(queue=4, now=1000.0)
    result = decide(config, state, [])
    assert result.action == "noop"
    assert "unknown" in result.reason.lower()


# ---------------------------------------------------------------------------
# Non-active members are excluded from decisions
# ---------------------------------------------------------------------------

def test_pending_members_not_counted_as_active():
    config = cfg(min_workers=1, max_workers=4, policy="queue_aware")
    state = st8(queue=0, now=1000.0)
    members = [
        make_member("m-pending", "T1", status="pending", joined_at=990.0),
    ]
    # pending member not counted → current=0 → target=min=1 → scale_up
    result = decide(config, state, members)
    assert result.action == "scale_up"
    assert result.delta == 1


def test_reaped_members_excluded():
    config = cfg(min_workers=1, max_workers=4, policy="queue_aware")
    state = st8(queue=0, now=1000.0)
    members = [
        make_member("m-reaped", "T1", status="reaped", joined_at=500.0, last_heartbeat=100.0),
        active("m-active", "T2", joined=900.0, heartbeat=990.0),
    ]
    result = decide(config, state, members)
    assert result.action == "noop"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def test_ceil_div_basic():
    assert _ceil_div(4, 2) == 2
    assert _ceil_div(5, 2) == 3
    assert _ceil_div(1, 2) == 1
    assert _ceil_div(0, 2) == 0


def test_is_stale_with_heartbeat():
    m = make_member(last_heartbeat=600.0, joined_at=500.0)
    assert _is_stale(m, now=1000.0, threshold=300.0) is True
    assert _is_stale(m, now=1000.0, threshold=500.0) is False


def test_is_stale_without_heartbeat():
    m = make_member(last_heartbeat=None, joined_at=600.0)
    assert _is_stale(m, now=1000.0, threshold=300.0) is True
    m2 = make_member(last_heartbeat=None, joined_at=900.0)
    assert _is_stale(m2, now=1000.0, threshold=300.0) is False


# ---------------------------------------------------------------------------
# Hypothesis property test: min ≤ current + delta ≤ max
# ---------------------------------------------------------------------------

_valid_providers = st.sampled_from(["claude", "codex", "gemini"])


@settings(max_examples=200)
@given(
    min_w=st.integers(min_value=0, max_value=5),
    max_w=st.integers(min_value=0, max_value=10),
    current=st.integers(min_value=0, max_value=12),
    queue=st.integers(min_value=0, max_value=20),
    policy=st.sampled_from(["fixed", "queue_aware"]),
    cooldown=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    elapsed_since_scale=st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
    has_last_scaled=st.booleans(),
)
def test_decision_invariant_delta_respects_bounds(
    min_w, max_w, current, queue, policy, cooldown, elapsed_since_scale, has_last_scaled
):
    assume(max_w >= min_w)
    assume(current <= 12)

    now = 1000.0
    last_scaled = (now - elapsed_since_scale) if has_last_scaled else None

    config = PoolConfig(
        pool_id="test",
        min_workers=min_w,
        max_workers=max_w,
        scaling_policy=policy,
        provider_mix=["claude"],
        cooldown_seconds=cooldown,
        heartbeat_stale_seconds=300.0,
    )

    # Build active members with fresh heartbeats (no stale => no reap path)
    members = [
        Membership(
            membership_id=f"m-{i}",
            terminal_id=f"T{i}",
            provider="claude",
            pool_role="backend-developer",
            status="active",
            joined_at=now - 10,
            last_heartbeat=now - 5,
        )
        for i in range(min(current, max_w + 2))
    ]
    actual_current = len(members)

    state = PoolState(queue_depth=queue, last_scaled_at=last_scaled, now=now)
    result = decide(config, state, members)

    if result.action in ("scale_up", "scale_down"):
        projected = actual_current + result.delta
        assert projected >= min_w, (
            f"projected={projected} < min_workers={min_w} "
            f"(action={result.action} delta={result.delta} current={actual_current})"
        )
        assert projected <= max_w, (
            f"projected={projected} > max_workers={max_w} "
            f"(action={result.action} delta={result.delta} current={actual_current})"
        )
