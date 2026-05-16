"""test_cost_aware_policy.py — Tests for cost_aware_v1 policy and cost_tracker.

Covers:
- cost_aware_v1 cost-ceiling hold on scale-up
- cost_aware_v1 proceeds when below ceiling
- cost_aware_v1 never blocks scale-down
- cost_aware_v1 with no ceiling defined
- cost_aware_v1 with ceiling=0 (disabled)
- recent_cost_per_hour from real NDJSON file
- recent_cost_per_hour window filtering
- recent_cost_per_hour with missing cost_usd fields
- recent_cost_per_hour on empty file
- _distribute_workers round-robin distribution
- VNX_STATE_DIR env override for receipts path

Wave 6 PR-6.4 — ADR-018 pluggable scaling policies.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_decision_engine import Membership, PoolConfig, PoolState  # noqa: E402
from pool_scaling_policies.cost_aware_v1 import cost_aware_v1      # noqa: E402
from cost_tracker import (                                           # noqa: E402
    recent_cost_per_hour,
    _distribute_workers,
    _read_last_lines,
    _resolve_receipts_path,
)

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from pool_state_fixtures import make_member, make_state  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cfg(
    min_workers: int = 1,
    max_workers: int = 4,
    policy: str = "cost_aware_v1",
    cost_ceiling: Optional[float] = None,
    provider_mix: Optional[List[str]] = None,
) -> PoolConfig:
    return PoolConfig(
        pool_id="default",
        min_workers=min_workers,
        max_workers=max_workers,
        scaling_policy=policy,
        provider_mix=provider_mix or ["claude"],
        cooldown_seconds=0.0,
        heartbeat_stale_seconds=300.0,
        cost_ceiling_usd=cost_ceiling,
    )


def st8(queue: int = 4) -> PoolState:
    return make_state(queue_depth=queue, last_scaled_at=None, now=1000.0)


def n_active(n: int) -> List[Membership]:
    return [
        make_member(
            membership_id=f"m-{i}",
            terminal_id=f"T{i}",
            status="active",
            joined_at=900.0 + i,
            last_heartbeat=990.0,
        )
        for i in range(n)
    ]


def _write_receipts(path: Path, receipts: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in receipts:
            f.write(json.dumps(r) + "\n")


def _now_iso(offset_minutes: int = 0) -> str:
    dt = datetime.now(tz=timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# cost_aware_v1: cost-ceiling enforcement
# ---------------------------------------------------------------------------

def test_cost_aware_holds_when_estimated_exceeds_ceiling():
    """When estimated hourly cost exceeds ceiling, hold at current size."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=0.01)
    state = st8(queue=4)  # queue_depth_v1 proposes target=2
    members = n_active(1)  # current=1 → scaling UP proposed

    # _estimate_hourly_cost falls back to target_size * 0.5 per worker.
    # proposed_target=2, fallback cost=2*0.5=1.0 > ceiling=0.01 → hold at 1.
    result = cost_aware_v1(config, state, members)
    assert result == 1  # held at current active count


def test_cost_aware_proceeds_when_below_ceiling():
    """When estimated cost is within ceiling, scaling proceeds."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=100.0)
    state = st8(queue=4)   # proposes target=2
    members = n_active(1)  # current=1

    # fallback cost=2*0.5=1.0 < ceiling=100.0 → proceed
    result = cost_aware_v1(config, state, members)
    assert result == 2


def test_cost_aware_ignores_ceiling_when_scaling_down():
    """Scale-down is never blocked, even with a tight ceiling."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=0.001)
    state = st8(queue=0)   # target=min=1
    members = n_active(3)  # current=3, scaling DOWN

    result = cost_aware_v1(config, state, members)
    assert result == 1  # scale-down to min, not held


def test_cost_aware_ignores_ceiling_when_already_at_target():
    """No hold when proposed target == current."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=0.001)
    state = st8(queue=2)   # ceil(2/2)=1 → target=1
    members = n_active(1)  # current=1, no change

    result = cost_aware_v1(config, state, members)
    assert result == 1  # at target, ceiling irrelevant


def test_cost_aware_no_ceiling_behaves_like_queue_depth_v1():
    """When cost_ceiling_usd is None, falls back to queue_depth_v1 target."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=None)
    state = st8(queue=4)
    members = n_active(0)

    result = cost_aware_v1(config, state, members)
    assert result == 2  # queue_depth_v1: ceil(4/2)=2


def test_cost_aware_zero_ceiling_bypassed():
    """cost_ceiling_usd=0 is treated as 'no ceiling defined' (disabled)."""
    config = make_cfg(min_workers=1, max_workers=4, cost_ceiling=0.0)
    state = st8(queue=4)
    members = n_active(0)

    result = cost_aware_v1(config, state, members)
    assert result == 2  # ceiling=0 → disabled → queue_depth_v1 target


# ---------------------------------------------------------------------------
# recent_cost_per_hour: reading from NDJSON
# ---------------------------------------------------------------------------

def test_recent_cost_per_hour_returns_zero_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    result = recent_cost_per_hour(workers=2, provider_mix=["claude"])
    assert result == 0.0


def test_recent_cost_per_hour_returns_zero_for_empty_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipts_path.write_text("")
    result = recent_cost_per_hour(workers=2, provider_mix=["claude"])
    assert result == 0.0


def test_recent_cost_per_hour_from_receipts(tmp_path, monkeypatch):
    """Reads cost_usd from recent receipts and computes hourly estimate."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    receipts_path = tmp_path / "t0_receipts.ndjson"

    receipts = [
        {"timestamp": _now_iso(-5),  "provider": "claude", "cost_usd": 0.10},
        {"timestamp": _now_iso(-10), "provider": "claude", "cost_usd": 0.20},
        {"timestamp": _now_iso(-15), "provider": "claude", "cost_usd": 0.30},
    ]
    _write_receipts(receipts_path, receipts)

    # avg cost = (0.10 + 0.20 + 0.30) / 3 = 0.20
    # hourly = 0.20 * 6 dispatches/hr * 1 worker = 1.20
    result = recent_cost_per_hour(workers=1, provider_mix=["claude"])
    assert abs(result - 1.20) < 0.01, f"expected ~1.20 got {result}"


def test_recent_cost_per_hour_filters_by_window(tmp_path, monkeypatch):
    """Receipts outside the window_minutes are ignored."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    receipts_path = tmp_path / "t0_receipts.ndjson"

    receipts = [
        {"timestamp": _now_iso(-5),   "provider": "claude", "cost_usd": 0.10},  # in window
        {"timestamp": _now_iso(-90),  "provider": "claude", "cost_usd": 9.99},  # outside 60m window
    ]
    _write_receipts(receipts_path, receipts)

    result = recent_cost_per_hour(workers=1, provider_mix=["claude"], window_minutes=60)
    # Only the in-window receipt counts: 0.10 * 6 = 0.60
    assert abs(result - 0.60) < 0.01, f"expected ~0.60 got {result}"


def test_recent_cost_per_hour_handles_missing_cost_usd(tmp_path, monkeypatch):
    """Lines without cost_usd are silently skipped."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    receipts_path = tmp_path / "t0_receipts.ndjson"

    receipts = [
        {"timestamp": _now_iso(-5), "provider": "claude"},                        # no cost_usd
        {"timestamp": _now_iso(-5), "provider": "claude", "cost_usd": None},      # null
        {"timestamp": _now_iso(-5), "provider": "claude", "cost_usd": 0.20},      # valid
    ]
    _write_receipts(receipts_path, receipts)

    result = recent_cost_per_hour(workers=1, provider_mix=["claude"])
    assert abs(result - 0.20 * 6.0) < 0.01


def test_recent_cost_per_hour_multi_provider(tmp_path, monkeypatch):
    """Per-provider averaging with a mixed provider_mix."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    receipts_path = tmp_path / "t0_receipts.ndjson"

    receipts = [
        {"timestamp": _now_iso(-5), "provider": "claude",  "cost_usd": 0.10},
        {"timestamp": _now_iso(-5), "provider": "codex",   "cost_usd": 0.20},
    ]
    _write_receipts(receipts_path, receipts)

    # 1 claude worker + 1 codex worker
    result = recent_cost_per_hour(workers=2, provider_mix=["claude", "codex"])
    # claude: 0.10 * 6 * 1 = 0.60; codex: 0.20 * 6 * 1 = 1.20; total = 1.80
    assert abs(result - 1.80) < 0.01, f"expected ~1.80 got {result}"


def test_recent_cost_per_hour_state_dir_override(tmp_path, monkeypatch):
    """VNX_STATE_DIR env var controls where receipts are read from."""
    custom_dir = tmp_path / "custom_state"
    monkeypatch.setenv("VNX_STATE_DIR", str(custom_dir))
    receipts_path = custom_dir / "t0_receipts.ndjson"

    receipts = [{"timestamp": _now_iso(-5), "provider": "claude", "cost_usd": 0.05}]
    _write_receipts(receipts_path, receipts)

    result = recent_cost_per_hour(workers=1, provider_mix=["claude"])
    assert result > 0.0


# ---------------------------------------------------------------------------
# _distribute_workers
# ---------------------------------------------------------------------------

def test_distribute_workers_single_provider():
    dist = _distribute_workers(3, ["claude"])
    assert dist == {"claude": 3}


def test_distribute_workers_round_robin_equal_split():
    dist = _distribute_workers(4, ["claude", "codex"])
    assert dist["claude"] == 2
    assert dist["codex"] == 2


def test_distribute_workers_round_robin_unequal():
    dist = _distribute_workers(3, ["claude", "codex"])
    assert dist["claude"] == 2
    assert dist["codex"] == 1


def test_distribute_workers_zero_total():
    dist = _distribute_workers(0, ["claude"])
    assert dist == {}


def test_distribute_workers_three_providers():
    dist = _distribute_workers(7, ["claude", "codex", "gemini"])
    assert sum(dist.values()) == 7
    assert dist["claude"] == 3    # slots 0, 3, 6
    assert dist["codex"] == 2     # slots 1, 4
    assert dist["gemini"] == 2    # slots 2, 5


# ---------------------------------------------------------------------------
# _read_last_lines
# ---------------------------------------------------------------------------

def test_read_last_lines_returns_last_n(tmp_path):
    p = tmp_path / "test.ndjson"
    lines = [f'{{"line": {i}}}' for i in range(50)]
    p.write_text("\n".join(lines) + "\n")
    result = _read_last_lines(p, n=10)
    assert len(result) == 10
    # Last line should be the 50th entry
    assert '"line": 49' in result[-1]


def test_read_last_lines_empty_file(tmp_path):
    p = tmp_path / "empty.ndjson"
    p.write_text("")
    result = _read_last_lines(p, n=10)
    assert result == []


def test_read_last_lines_fewer_than_n(tmp_path):
    p = tmp_path / "small.ndjson"
    p.write_text('{"a": 1}\n{"b": 2}\n')
    result = _read_last_lines(p, n=100)
    assert len(result) == 2
