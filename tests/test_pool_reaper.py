"""test_pool_reaper.py — Pure unit tests for pool_reaper.identify_reap_targets.

No SQLite, no subprocess. Tests the pure detection logic + pid-kill guard
on PoolManager._kill_subprocess.

Wave 6 PR-6.6 — Health monitoring + dead-worker reap.
"""

from __future__ import annotations

import sys
import sqlite3
import unittest.mock
from pathlib import Path
from typing import List, Optional

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_reaper import ReapConfig, ReapTarget, identify_reap_targets  # noqa: E402
from pool_state_fixtures import make_member, create_test_db_file  # noqa: E402
from pool_manager import PoolManager, SpawnResult  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _always_succeed(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=True)


def _make_manager(db_path: Path) -> PoolManager:
    return PoolManager(
        project_id="vnx-dev",
        pool_id="default",
        db_path=db_path,
        spawn_fn=_always_succeed,
    )


def _member(
    membership_id: str = "m-001",
    terminal_id: str = "T1",
    status: str = "active",
    joined_at: float = 0.0,
    last_heartbeat: Optional[float] = None,
) -> object:
    return make_member(
        membership_id=membership_id,
        terminal_id=terminal_id,
        status=status,
        joined_at=joined_at,
        last_heartbeat=last_heartbeat,
    )


DEFAULT_CFG = ReapConfig(
    heartbeat_stale_threshold_s=180.0,
    warmup_window_s=120.0,
)


# ---------------------------------------------------------------------------
# 1. Warmup window exemption
# ---------------------------------------------------------------------------

def test_active_within_warmup_exempt():
    """Worker younger than warmup_window is never reaped, even without heartbeat."""
    now = 1000.0
    m = _member(joined_at=900.0, last_heartbeat=None)  # age = 100s < 120s warmup
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


def test_active_at_warmup_boundary_not_exempt():
    """Worker exactly at warmup boundary (age == warmup_window_s) is NOT exempt."""
    now = 1000.0
    # age = 880.0 = 120s (exactly at boundary, condition is <, so NOT exempt)
    m = _member(joined_at=now - 120.0, last_heartbeat=None)
    # age = 120s > heartbeat_stale_threshold_s=180? No, 120 < 180, still not reaped.
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


# ---------------------------------------------------------------------------
# 2. Stale heartbeat detection
# ---------------------------------------------------------------------------

def test_active_past_warmup_stale_heartbeat_reaped():
    """Worker past warmup with stale heartbeat (>180s ago) is reap-eligible."""
    now = 1000.0
    m = _member(
        joined_at=200.0,        # age = 800s, well past warmup
        last_heartbeat=810.0,   # stale_age = 190s > 180s
    )
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert len(targets) == 1
    assert targets[0].membership_id == "m-001"
    assert "heartbeat_stale" in targets[0].reason


def test_active_past_warmup_fresh_heartbeat_not_reaped():
    """Worker past warmup with fresh heartbeat (<180s ago) is NOT reap-eligible."""
    now = 1000.0
    m = _member(
        joined_at=200.0,        # age = 800s
        last_heartbeat=870.0,   # stale_age = 130s < 180s → fresh
    )
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


# ---------------------------------------------------------------------------
# 3. Never-heartbeat detection
# ---------------------------------------------------------------------------

def test_active_never_heartbeat_age_exceeds_threshold_reaped():
    """Worker with no heartbeat and age > threshold is reap-eligible."""
    now = 1000.0
    m = _member(joined_at=700.0, last_heartbeat=None)  # age = 300s > 180s
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert len(targets) == 1
    assert "never_heartbeat" in targets[0].reason


def test_active_never_heartbeat_age_within_threshold_not_reaped():
    """Worker with no heartbeat but age <= threshold is NOT reap-eligible."""
    now = 1000.0
    m = _member(joined_at=850.0, last_heartbeat=None)  # age = 150s < 180s (but > 120s warmup)
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


# ---------------------------------------------------------------------------
# 4. Non-active members skip
# ---------------------------------------------------------------------------

def test_drained_member_not_reap_eligible():
    """Members with status=draining are skipped."""
    now = 1000.0
    m = _member(status="draining", joined_at=0.0, last_heartbeat=None)
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


def test_reaped_member_not_double_reaped():
    """Members already status=reaped are skipped."""
    now = 1000.0
    m = _member(status="reaped", joined_at=0.0, last_heartbeat=None)
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


def test_pending_member_not_reap_eligible():
    """Members with status=pending are skipped."""
    now = 1000.0
    m = _member(status="pending", joined_at=0.0, last_heartbeat=None)
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert targets == []


# ---------------------------------------------------------------------------
# 5. Configuration customisation
# ---------------------------------------------------------------------------

def test_threshold_customization_heartbeat():
    """Custom heartbeat_stale_threshold_s is respected."""
    now = 1000.0
    tight_cfg = ReapConfig(heartbeat_stale_threshold_s=60.0, warmup_window_s=10.0)
    m = _member(joined_at=900.0, last_heartbeat=930.0)  # stale_age = 70s > 60s
    targets = identify_reap_targets([m], now, tight_cfg)
    assert len(targets) == 1


def test_threshold_customization_warmup():
    """Custom warmup_window_s is respected — longer warmup protects young workers."""
    now = 1000.0
    long_warmup_cfg = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=600.0)
    m = _member(joined_at=500.0, last_heartbeat=None)  # age = 500s, within 600s warmup
    targets = identify_reap_targets([m], now, long_warmup_cfg)
    assert targets == []


# ---------------------------------------------------------------------------
# 6. Multiple members
# ---------------------------------------------------------------------------

def test_multiple_members_mixed_eligibility():
    """Only stale members are identified; fresh ones are skipped."""
    now = 1000.0
    fresh = _member("fresh", "T1", "active", joined_at=200.0, last_heartbeat=870.0)  # 130s stale
    stale = _member("stale", "T2", "active", joined_at=200.0, last_heartbeat=810.0)  # 190s stale
    targets = identify_reap_targets([fresh, stale], now, DEFAULT_CFG)
    assert len(targets) == 1
    assert targets[0].membership_id == "stale"


def test_empty_members_returns_empty():
    """Empty member list produces no reap targets."""
    targets = identify_reap_targets([], 1000.0, DEFAULT_CFG)
    assert targets == []


# ---------------------------------------------------------------------------
# 7. Reason string format
# ---------------------------------------------------------------------------

def test_reason_includes_threshold_for_heartbeat_stale():
    """Reason string includes measured stale age and threshold."""
    now = 1000.0
    m = _member(joined_at=200.0, last_heartbeat=810.0)  # 190s stale
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert len(targets) == 1
    reason = targets[0].reason
    assert "heartbeat_stale" in reason
    assert "190" in reason
    assert "180" in reason


def test_reason_includes_threshold_for_never_heartbeat():
    """Reason string includes worker age and threshold for never-heartbeat case."""
    now = 1000.0
    m = _member(joined_at=700.0, last_heartbeat=None)  # age = 300s
    targets = identify_reap_targets([m], now, DEFAULT_CFG)
    assert len(targets) == 1
    reason = targets[0].reason
    assert "never_heartbeat" in reason
    assert "300" in reason
    assert "180" in reason


# ---------------------------------------------------------------------------
# 8. ReapConfig defaults
# ---------------------------------------------------------------------------

def test_reap_config_defaults():
    """Default ReapConfig has expected threshold values."""
    cfg = ReapConfig()
    assert cfg.heartbeat_stale_threshold_s == 180.0
    assert cfg.stuck_threshold_s == 300.0
    assert cfg.warmup_window_s == 120.0


# ---------------------------------------------------------------------------
# 9. pid <= 0 security invariant — tested via PoolManager._kill_subprocess
# ---------------------------------------------------------------------------

def test_pid_none_not_killed(tmp_path):
    """pid=None must never result in os.kill() being called."""
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "test.db")
    mgr = _make_manager(db)

    with unittest.mock.patch("pool_manager.os.kill") as mock_kill:
        mgr._kill_subprocess("T1", pid=None)

    mock_kill.assert_not_called()


def test_pid_zero_not_killed(tmp_path):
    """pid=0 must never result in os.kill() being called (would kill process group)."""
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "test.db")
    mgr = _make_manager(db)

    with unittest.mock.patch("pool_manager.os.kill") as mock_kill:
        mgr._kill_subprocess("T1", pid=0)

    mock_kill.assert_not_called()


def test_pid_negative_not_killed(tmp_path):
    """pid=-1 must never result in os.kill() being called (would signal all processes)."""
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "test.db")
    mgr = _make_manager(db)

    with unittest.mock.patch("pool_manager.os.kill") as mock_kill:
        mgr._kill_subprocess("T1", pid=-1)

    mock_kill.assert_not_called()
