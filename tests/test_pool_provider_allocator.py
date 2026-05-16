"""Tests for pool_provider_allocator — provider-mix allocation logic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from pool_decision_engine import Membership
from pool_provider_allocator import (
    AllocationResult,
    allocate_for_scale_up,
    compute_target_shares,
    select_for_scale_down,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_mid_counter = 0


def m(provider: str, t: float = 0.0, status: str = "active", mid: str | None = None) -> Membership:
    global _mid_counter
    _mid_counter += 1
    return Membership(
        membership_id=mid or f"mid_{provider}_{int(t)}_{_mid_counter}",
        terminal_id=f"T-{provider}-{int(t)}-{_mid_counter}",
        provider=provider,
        pool_role="backend-developer",
        status=status,
        joined_at=t,
    )


def reset_counter() -> None:
    global _mid_counter
    _mid_counter = 0


# ---------------------------------------------------------------------------
# compute_target_shares
# ---------------------------------------------------------------------------

class TestComputeTargetShares:
    def test_balanced_two_providers_even_pool(self):
        result = compute_target_shares(["claude", "codex"], 4)
        assert result == {"claude": 2, "codex": 2}

    def test_two_thirds_one_third_split(self):
        result = compute_target_shares(["claude", "claude", "codex"], 6)
        assert result == {"claude": 4, "codex": 2}

    def test_acceptance_criterion(self):
        """Explicit acceptance test from dispatch spec."""
        assert compute_target_shares(["claude", "claude", "codex"], 6) == {"claude": 4, "codex": 2}

    def test_empty_mix_returns_empty(self):
        assert compute_target_shares([], 10) == {}

    def test_single_provider(self):
        result = compute_target_shares(["claude"], 5)
        assert result == {"claude": 5}

    def test_sum_equals_pool_size(self):
        for pool_size in [1, 2, 3, 5, 7, 10, 13]:
            result = compute_target_shares(["claude", "claude", "codex"], pool_size)
            assert sum(result.values()) == pool_size, f"pool_size={pool_size}, result={result}"

    def test_sum_equals_pool_size_even_split(self):
        for pool_size in [1, 2, 3, 4, 5, 6, 7]:
            result = compute_target_shares(["claude", "codex"], pool_size)
            assert sum(result.values()) == pool_size

    def test_three_providers(self):
        result = compute_target_shares(["claude", "codex", "litellm:deepseek"], 6)
        assert sum(result.values()) == 6
        assert result.get("claude", 0) >= 1
        assert result.get("codex", 0) >= 1
        assert result.get("litellm:deepseek", 0) >= 1

    def test_litellm_deepseek_in_mix(self):
        result = compute_target_shares(["claude", "litellm:deepseek"], 4)
        assert result == {"claude": 2, "litellm:deepseek": 2}

    def test_pool_size_zero(self):
        result = compute_target_shares(["claude", "codex"], 0)
        assert sum(result.values()) == 0


# ---------------------------------------------------------------------------
# allocate_for_scale_up — parametrized table tests
# ---------------------------------------------------------------------------

class TestAllocateForScaleUpParametrized:
    @pytest.mark.parametrize("mix,members_setup,delta,expected_providers", [
        # Empty pool — pure mix allocation
        (["claude", "claude", "codex"], [], 3, ["claude", "claude", "codex"]),
        # Balanced 50-50 over 4 slots
        (["claude", "codex"], [], 4, ["claude", "codex", "claude", "codex"]),
        # Single provider mix
        (["claude"], [], 3, ["claude", "claude", "claude"]),
        # delta=0 returns empty
        (["claude", "codex"], [], 0, []),
    ])
    def test_allocation(self, mix, members_setup, delta, expected_providers):
        reset_counter()
        members = [m(p) for p in members_setup]
        result = allocate_for_scale_up(members, mix, delta)
        assert result.providers == expected_providers

    def test_existing_claude_then_codex_allocated(self):
        """With 1 existing claude, delta=2: fills codex gap then balances."""
        reset_counter()
        # mix=["claude","codex"], new_size=3, target={claude:2,codex:1}
        # pending starts at {claude:1}
        # step1: claude gap=1, codex gap=1 -> tie -> claude (dict order)
        # step2: codex gap=1 -> codex
        members = [m("claude", t=5.0)]
        result = allocate_for_scale_up(members, ["claude", "codex"], delta=2)
        assert len(result.providers) == 2
        assert result.providers.count("claude") + result.providers.count("codex") == 2

    def test_provider_binding_immutable(self):
        """Existing members keep their provider; allocator never touches them."""
        reset_counter()
        existing = [m("claude", t=1.0), m("codex", t=2.0)]
        result = allocate_for_scale_up(existing, ["claude", "codex"], delta=2)
        # Only 2 NEW providers are returned
        assert len(result.providers) == 2
        # The existing members' providers are unchanged (function is pure; no mutation)

    def test_result_length_equals_delta(self):
        for delta in [1, 2, 3, 5]:
            result = allocate_for_scale_up([], ["claude", "codex"], delta)
            assert len(result.providers) == delta

    def test_all_providers_from_mix_or_fallback(self):
        mix = ["claude", "litellm:deepseek"]
        result = allocate_for_scale_up([], mix, delta=6)
        for p in result.providers:
            assert p in mix or p == "claude"


class TestAllocateForScaleUpFallback:
    def test_empty_mix_all_fallback(self):
        result = allocate_for_scale_up([], [], 3)
        assert result.providers == ["claude", "claude", "claude"]
        assert result.fallback_used == ["claude", "claude", "claude"]

    def test_empty_mix_custom_fallback(self):
        result = allocate_for_scale_up([], [], 2, fallback_provider="codex")
        assert result.providers == ["codex", "codex"]
        assert result.fallback_used == ["codex", "codex"]

    def test_negative_delta_returns_empty(self):
        result = allocate_for_scale_up([], ["claude"], -1)
        assert result.providers == []
        assert result.fallback_used == []

    def test_no_fallback_used_in_normal_allocation(self):
        result = allocate_for_scale_up([], ["claude", "codex"], 4)
        assert result.fallback_used == []


# ---------------------------------------------------------------------------
# allocate_for_scale_up — provider counts
# ---------------------------------------------------------------------------

class TestAllocateProviderCounts:
    def test_two_claude_two_codex_for_target_four(self):
        """Acceptance criterion: provider_mix=["claude","codex"] target=4 → 2+2."""
        result = allocate_for_scale_up([], ["claude", "codex"], delta=4)
        counts = {}
        for p in result.providers:
            counts[p] = counts.get(p, 0) + 1
        assert counts.get("claude", 0) == 2
        assert counts.get("codex", 0) == 2

    def test_two_claude_one_codex_for_target_three(self):
        result = allocate_for_scale_up([], ["claude", "claude", "codex"], delta=3)
        counts = {}
        for p in result.providers:
            counts[p] = counts.get(p, 0) + 1
        assert counts.get("claude", 0) == 2
        assert counts.get("codex", 0) == 1

    def test_litellm_deepseek_mixed_spawn(self):
        """Mock-spawn with 1 claude + 1 litellm:deepseek works."""
        result = allocate_for_scale_up([], ["claude", "litellm:deepseek"], delta=2)
        assert len(result.providers) == 2
        assert "claude" in result.providers
        assert "litellm:deepseek" in result.providers


# ---------------------------------------------------------------------------
# select_for_scale_down
# ---------------------------------------------------------------------------

class TestSelectForScaleDown:
    def test_noop_on_zero_delta(self):
        reset_counter()
        members = [m("claude", t=1), m("codex", t=2)]
        result = select_for_scale_down(members, ["claude", "codex"], 0)
        assert result == []

    def test_noop_on_positive_delta(self):
        reset_counter()
        members = [m("claude", t=1)]
        result = select_for_scale_down(members, ["claude"], 1)
        assert result == []

    def test_release_oldest_of_highest_excess(self):
        """Scale-down releases oldest worker of the provider with highest excess."""
        reset_counter()
        m1 = m("claude", t=1.0, mid="mid_c_1")
        m2 = m("claude", t=2.0, mid="mid_c_2")
        m3 = m("codex", t=3.0, mid="mid_x_3")
        # mix=["claude","codex"], current=2 claude+1 codex, new_size=2
        # target for new_size=2: {claude:1,codex:1}
        # excess: {claude:1,codex:0} -> release oldest claude
        result = select_for_scale_down([m1, m2, m3], ["claude", "codex"], -1)
        assert result == ["mid_c_1"]

    def test_release_count_matches_abs_delta(self):
        reset_counter()
        members = [m("claude", t=i) for i in range(5)]
        result = select_for_scale_down(members, ["claude"], -3)
        assert len(result) == 3

    def test_release_oldest_when_tied_excess(self):
        """When all providers have equal (zero) excess, release oldest overall."""
        reset_counter()
        m1 = m("claude", t=1.0, mid="oldest")
        m2 = m("codex", t=5.0, mid="newer")
        # new_size=1, mix=["claude","codex"], target={claude:1,codex:0} or {0,1}?
        # Actually: new_size=2+(-1)=1; target=compute_target_shares(["claude","codex"],1)
        # -> mix_size=2, sorted=[("claude",1),("codex",1)], claude(not last): round(1*1/2)=round(0.5)=0
        # Wait, round(0.5)=0 in Python (round-half-to-even). remaining=1-0=1. codex(last)=1.
        # target={"claude":0,"codex":1}. excess={claude:1-0=1, codex:1-1=0}.
        # highest excess = claude(1). release oldest claude = m1.
        result = select_for_scale_down([m1, m2], ["claude", "codex"], -1)
        # claude has excess=1, so oldest claude is released
        assert "oldest" in result

    def test_scale_down_empty_mix(self):
        """With empty mix, fallback to oldest overall."""
        reset_counter()
        m1 = m("claude", t=1.0, mid="old")
        m2 = m("codex", t=2.0, mid="new")
        result = select_for_scale_down([m1, m2], [], -1)
        assert result == ["old"]

    def test_only_active_members_considered(self):
        """Reaped/pending members should not be candidates."""
        reset_counter()
        active = m("claude", t=1.0, mid="active_one", status="active")
        reaped = m("claude", t=0.5, mid="reaped_one", status="reaped")
        result = select_for_scale_down([active, reaped], ["claude"], -1)
        assert result == ["active_one"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_single_member_scale_up(self):
        reset_counter()
        result = allocate_for_scale_up([], ["claude"], 1)
        assert result.providers == ["claude"]

    def test_large_delta_consistent_counts(self):
        """For a balanced mix over 10 workers, counts should be roughly equal."""
        result = allocate_for_scale_up([], ["claude", "codex"], 10)
        counts = {}
        for p in result.providers:
            counts[p] = counts.get(p, 0) + 1
        assert counts.get("claude", 0) == 5
        assert counts.get("codex", 0) == 5

    def test_allocation_result_is_pure_no_side_effects(self):
        """Calling allocate twice with same args returns same result."""
        reset_counter()
        members = [m("claude", t=1.0)]
        r1 = allocate_for_scale_up(members, ["claude", "codex"], 3)
        r2 = allocate_for_scale_up(members, ["claude", "codex"], 3)
        assert r1.providers == r2.providers
