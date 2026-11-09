"""Tests for scripts/lib/slot/slot_memory.py (RS-5, rekenslot-per-machine).

Each item of "Klaar per levering" 6 is its own test. The module is pure over
the ADR-062 state dict; only ``take_sample`` touches the outside world, and its
runner is a fake here, so no test needs a live host.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib"

SWAP_TEXT = "vm.swapusage: total = 5120.00M  used = 3813.62M  free = 1306.38M  (encrypted)\n"
VM_STAT_TEXT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages occupied by compressor:                 589824.\n")  # the parser needs the dot

POLICY = {
    "profiles": {
        "seo-gate": {"heavy": True},
        "builder-tests": {"heavy": True},
        "interactive": {"heavy": False},
    },
    "memory": {"swap_growth_mb": 256, "swap_level_mb": 3000, "compressor_gb": 8},
}


def sample(at: int, swap, compressor, status: str = "ok") -> dict:
    return {"measured_at": at, "swap_used_mb": swap, "compressor_gb": compressor,
            "status": status}


@pytest.fixture(scope="module")
def mod():
    """Import the library the way a caller with only scripts/lib on sys.path does."""
    sys.path.insert(0, str(LIB))
    try:
        from slot import slot_memory
    finally:
        sys.path.remove(str(LIB))
    return slot_memory


def test_bucket_start_rounds_down_to_30_seconds(mod):
    assert mod.bucket_start(1_000_020.9) == 1_000_020
    assert mod.bucket_start(1_000_049) == 1_000_020
    assert mod.bucket_start(1_000_050) == 1_000_050


def test_four_writers_in_one_bucket_leave_exactly_one_sample(mod):
    state = {}
    now = 1_000_000.0            # ten seconds into the bucket of 999_990
    wrote = [mod.record_sample(state, sample(int(now) + i, 100 + i, 1.0), now + i)
             for i in range(4)]
    assert wrote == [True, False, False, False]
    assert [s["bucket_start"] for s in state["samples"]] == [999_990]
    assert state["samples"][0]["swap_used_mb"] == 100   # the first writer wins
    assert mod.needs_own_sample(state, now) is False
    assert mod.needs_own_sample(state, now + mod.BUCKET_SECONDS) is True


def test_retention_keeps_the_last_600_seconds_whatever_the_count(mod):
    state = {}
    now = 1_000_020.0            # a bucket boundary
    for i in range(41):          # 20 minutes, one sample per bucket
        at = now - 1200 + i * mod.BUCKET_SECONDS
        assert mod.record_sample(state, sample(int(at), 10, 1.0), at)
    assert [s["bucket_start"] for s in state["samples"]] == list(
        range(int(now) - 600, int(now) + 1, mod.BUCKET_SECONDS))
    assert len(state["samples"]) == 21   # time, not a ring of 20


def test_too_little_history_falls_back_to_the_level_rule(mod):
    now = 1_000_020.0
    state = {}
    mod.record_sample(state, sample(int(now), 3100, 8.5), now)   # one usable sample
    assert mod.memory_verdict(state, POLICY, "builder-tests", now) == (
        mod.WAIT, [mod.MEMORY_GUARD])                            # swap and compressor both high
    other = {}
    mod.record_sample(other, sample(int(now), 2999, 9.0), now)   # swap just below the level
    assert mod.memory_verdict(other, POLICY, "builder-tests", now) == (mod.ADMIT, [])
    third = {}
    mod.record_sample(third, sample(int(now), 3500, 7.9), now)   # compressor just below it
    assert mod.memory_verdict(third, POLICY, "builder-tests", now) == (mod.ADMIT, [])


def test_growth_rule_needs_two_usable_samples_four_minutes_apart(mod):
    now = 1_000_020.0
    state = {}
    mod.record_sample(state, sample(int(now - 240), 1000, 8.5), now - 240)
    mod.record_sample(state, sample(int(now), 1300, 8.5), now)    # +300 MB in 4 min
    assert mod.memory_verdict(state, POLICY, "seo-gate", now) == (mod.WAIT, [mod.MEMORY_GUARD])
    close = {}
    mod.record_sample(close, sample(int(now - 180), 1000, 8.5), now - 180)
    mod.record_sample(close, sample(int(now), 1300, 8.5), now)    # 3 minutes apart: level only
    assert mod.memory_verdict(close, POLICY, "seo-gate", now) == (mod.ADMIT, [])


def test_recovery_needs_two_clean_samples_from_two_buckets(mod):
    now = 1_000_020.0
    state = {}
    guard_at = now - 600
    mod.record_sample(state, sample(int(guard_at), 3500, 8.5), guard_at)
    assert mod.memory_verdict(state, POLICY, "seo-gate", guard_at) == (
        mod.WAIT, [mod.MEMORY_GUARD])
    assert state["memory_recovery"] == {"blocked": True, "clean_buckets": []}
    first = now - 60
    mod.record_sample(state, sample(int(first), 100, 1.0), first)
    assert mod.memory_verdict(state, POLICY, "seo-gate", first) == (
        mod.WAIT, [mod.MEMORY_GUARD])                            # one clean bucket is not enough
    assert state["memory_recovery"]["clean_buckets"] == [mod.bucket_start(first)]
    assert mod.memory_verdict(state, POLICY, "seo-gate", first) == (
        mod.WAIT, [mod.MEMORY_GUARD])                            # one bucket counts once
    assert state["memory_recovery"]["clean_buckets"] == [mod.bucket_start(first)]
    second = first + mod.BUCKET_SECONDS
    mod.record_sample(state, sample(int(second), 100, 1.0), second)
    assert mod.memory_verdict(state, POLICY, "seo-gate", second) == (mod.ADMIT, [])
    assert state["memory_recovery"] == {"blocked": False, "clean_buckets": []}


@pytest.mark.parametrize("between", ["hot", "unavailable"])
def test_a_recorded_hot_or_unavailable_sample_restarts_recovery(mod, between):
    """Recovery counts recorded samples, not the moments someone evaluated."""
    now = 1_000_020.0
    state = {}
    mod.record_sample(state, sample(int(now), 3500, 8.5), now)
    assert mod.memory_verdict(state, POLICY, "seo-gate", now) == (mod.WAIT, [mod.MEMORY_GUARD])
    first = now + mod.BUCKET_SECONDS
    mod.record_sample(state, sample(int(first), 100, 1.0), first)
    assert mod.memory_verdict(state, POLICY, "seo-gate", first) == (mod.WAIT, [mod.MEMORY_GUARD])
    middle = first + mod.BUCKET_SECONDS
    bad = (sample(int(middle), 3500, 8.5) if between == "hot"
           else sample(int(middle), None, None, status="unavailable"))
    mod.record_sample(state, bad, middle)               # another writer, never evaluated
    latest = middle + mod.BUCKET_SECONDS
    mod.record_sample(state, sample(int(latest), 100, 1.0), latest)
    assert mod.memory_verdict(state, POLICY, "seo-gate", latest) == (
        mod.WAIT, [mod.MEMORY_GUARD])
    assert state["memory_recovery"] == {"blocked": True,
                                        "clean_buckets": [mod.bucket_start(latest)]}


@pytest.mark.parametrize("evaluate_each", [True, False])
def test_two_consecutive_clean_recorded_samples_recover(mod, evaluate_each):
    """Evaluated one by one, or recorded by another writer and then evaluated once."""
    now = 1_000_020.0
    state = {}
    mod.record_sample(state, sample(int(now), 3500, 8.5), now)
    assert mod.memory_verdict(state, POLICY, "seo-gate", now) == (mod.WAIT, [mod.MEMORY_GUARD])
    for offset in (mod.BUCKET_SECONDS, 2 * mod.BUCKET_SECONDS):
        at = now + offset
        mod.record_sample(state, sample(int(at), 100, 1.0), at)
        if evaluate_each or offset == 2 * mod.BUCKET_SECONDS:
            verdict = mod.memory_verdict(state, POLICY, "seo-gate", at)
    assert verdict == (mod.ADMIT, [])
    assert state["memory_recovery"] == {"blocked": False, "clean_buckets": []}


def test_a_heavy_nested_lease_under_a_light_parent_is_checked(mod):
    """Heavy belongs to the request: the function only knows its profile."""
    now = 1_000_020.0
    state = {"holders": {"outer": {"lease_id": "outer", "profile": "interactive"}},
             "samples": []}
    mod.record_sample(state, sample(int(now), 3200, 8.5), now)
    assert mod.memory_verdict(state, POLICY, "seo-gate", now) == (mod.WAIT, [mod.MEMORY_GUARD])
    assert mod.memory_verdict(state, POLICY, "interactive", now) == (mod.ADMIT, [])


def test_a_missing_measurement_waits_then_refuses_with_75(mod):
    now = 1_000_020.0
    state = {}
    assert mod.memory_verdict(state, POLICY, "builder-tests", now) == (
        mod.WAIT, [mod.MEMORY_METRIC_UNAVAILABLE])
    assert mod.memory_refusal(1799, 1800, [mod.MEMORY_METRIC_UNAVAILABLE]) is None
    assert mod.memory_refusal(1800, 1800, [mod.MEMORY_METRIC_UNAVAILABLE]) == 75
    mod.record_sample(state, sample(int(now), None, None, status="unavailable"), now)
    assert (state["samples"][0]["swap_used_mb"], state["samples"][0]["compressor_gb"]) == (
        None, None)
    assert mod.memory_verdict(state, POLICY, "builder-tests", now) == (
        mod.WAIT, [mod.MEMORY_METRIC_UNAVAILABLE])
    stale = {"samples": [{"bucket_start": 999_870, "measured_at": int(now) - 150,
                          "swap_used_mb": 10, "compressor_gb": 1.0, "status": "ok"}]}
    assert mod.memory_verdict(stale, POLICY, "builder-tests", now) == (
        mod.WAIT, [mod.MEMORY_METRIC_UNAVAILABLE])
    unknown = {"samples": [{"bucket_start": mod.bucket_start(now), "measured_at": int(now),
                            "swap_used_mb": 10, "compressor_gb": 1.0, "status": "OK"}]}
    assert mod.memory_verdict(unknown, POLICY, "builder-tests", now) == (
        mod.WAIT, [mod.MEMORY_METRIC_UNAVAILABLE])               # an unknown status is not ok
    broken = sample(int(now) + mod.BUCKET_SECONDS, 10, None)     # claims ok, one value missing
    mod.record_sample(state, broken, now + mod.BUCKET_SECONDS)
    assert (state["samples"][-1]["status"], state["samples"][-1]["swap_used_mb"]) == (
        "unavailable", None)
    # Another reason in the way means the memory component does not claim the refusal.
    assert mod.memory_refusal(1800, 1800, [mod.MEMORY_GUARD, "budget_full"]) is None
    assert mod.memory_refusal(1800, 1800, []) is None


def test_a_non_heavy_profile_is_never_held(mod):
    now = 1_000_020.0
    state = {}
    assert mod.memory_verdict(state, POLICY, "interactive", now) == (mod.ADMIT, [])
    assert "memory_recovery" not in state
    hot = {"samples": [{"bucket_start": 999_990, "measured_at": 1_000_000,
                        "swap_used_mb": 5000, "compressor_gb": 12.0, "status": "ok"}]}
    assert mod.memory_verdict(hot, POLICY, "interactive", now) == (mod.ADMIT, [])


def test_take_sample_reads_both_commands_through_the_runner(mod):
    seen = []

    def run(argv):
        seen.append(argv)
        return SWAP_TEXT if argv[-1] == "vm.swapusage" else VM_STAT_TEXT

    taken = mod.take_sample(run, now=1_000_020)
    assert seen == [["sysctl", "vm.swapusage"], ["vm_stat"]]
    assert taken["status"] == "ok"
    assert taken["measured_at"] == 1_000_020
    assert taken["swap_used_mb"] == pytest.approx(3813.62)
    assert taken["compressor_gb"] == pytest.approx(589824 * 16384 / 1024 ** 3)


def test_take_sample_of_a_failing_runner_is_unavailable_never_zero(mod):
    def boom(argv):
        raise OSError("sysctl: not found")

    assert mod.take_sample(boom, now=1_000_020) == {
        "measured_at": 1_000_020, "swap_used_mb": None,
        "compressor_gb": None, "status": "unavailable"}
    for run in (lambda argv: "nonsense", lambda argv: None):
        failed = mod.take_sample(run, now=1_000_020)
        assert failed["status"] == "unavailable"
        assert failed["swap_used_mb"] is None and failed["compressor_gb"] is None
