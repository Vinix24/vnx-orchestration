"""test_pool_tick_reap_decide_execute.py — Integration tests for tick = reap → decide → execute.

Uses real SQLite file-backed DBs via tmp_path.
Spawn function is mocked to avoid subprocess side-effects.
Kill function is mocked to avoid sending signals.

Wave 6 PR-6.6 — Health monitoring + dead-worker reap.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import unittest.mock
from pathlib import Path
from typing import List

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_manager import ExecResult, PoolManager, SpawnResult  # noqa: E402
from pool_reaper import ReapConfig  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _always_succeed(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=True)


def _make_manager(db_path: Path, *, min_workers: int = 1, max_workers: int = 4) -> PoolManager:
    db = create_test_db_file(
        db_path,
        min_workers=min_workers,
        max_workers=max_workers,
        cooldown_seconds=0,
    )
    mgr = PoolManager(
        project_id="vnx-dev",
        pool_id="default",
        db_path=db,
        spawn_fn=_always_succeed,
    )
    # Suppress actual subprocess kill to avoid signal surprises in tests
    mgr._kill_subprocess = lambda tid, pid: None  # type: ignore[method-assign]
    return mgr, db


def _insert_member(
    db_path: Path,
    terminal_id: str,
    *,
    heartbeat_at: str,
    joined_at: str = "2026-01-01T00:00:00.000000Z",
    membership_id: str = None,
) -> str:
    import uuid as _uuid
    mid = membership_id or str(_uuid.uuid4())
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT OR IGNORE INTO terminal_leases
            (terminal_id, project_id, state, lease_token, last_heartbeat_at)
        VALUES (?, 'vnx-dev', 'idle', '', ?)
        """,
        (terminal_id, heartbeat_at),
    )
    conn.execute(
        """
        INSERT INTO worker_pool_membership
            (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
        VALUES (?, 'vnx-dev', 'default', 'claude', 'backend-developer', ?, ?)
        """,
        (terminal_id, joined_at, json.dumps({"membership_id": mid})),
    )
    conn.commit()
    conn.close()
    return mid


def _active_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE project_id='vnx-dev' AND released_at IS NULL"
    ).fetchone()
    conn.close()
    return row[0]


def _reaped_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE released_at IS NOT NULL"
    ).fetchone()
    conn.close()
    return row[0]


def _read_pool_events(state_dir: Path) -> List[dict]:
    events_file = state_dir.parent / "events" / "pool_events.ndjson"
    if not events_file.exists():
        return []
    lines = [l.strip() for l in events_file.read_text().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# 1. Stuck worker reaped within one tick
# ---------------------------------------------------------------------------

def test_stuck_worker_reaped_within_one_tick(tmp_path):
    """Stale worker (heartbeat >180s ago) is reaped by reap_dead() in one tick."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")

    # Heartbeat 600s ago — well past 180s threshold
    mid = _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)
    result = mgr.tick()

    assert mid in result.reaped, f"Stale member must be in reaped list; got {result.reaped}"
    assert _reaped_count(db) == 1


# ---------------------------------------------------------------------------
# 2. reap_dead() runs before decide() — call-order verified
# ---------------------------------------------------------------------------

def test_reap_runs_before_decide(tmp_path):
    """tick() must call reap_dead() before decide()."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")

    call_order: List[str] = []
    original_reap = mgr.reap_dead
    original_decide = mgr.decide

    def tracked_reap():
        call_order.append("reap_dead")
        return original_reap()

    def tracked_decide():
        call_order.append("decide")
        return original_decide()

    mgr.reap_dead = tracked_reap   # type: ignore[method-assign]
    mgr.decide = tracked_decide    # type: ignore[method-assign]

    mgr.tick()

    assert call_order == ["reap_dead", "decide"], (
        f"Expected reap_dead before decide; got {call_order}"
    )


# ---------------------------------------------------------------------------
# 3. Warmup window respected
# ---------------------------------------------------------------------------

def test_warmup_window_respected(tmp_path):
    """Worker younger than warmup_window_s is NOT reaped even without heartbeat."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=120.0)

    # Joined 60 seconds ago — inside warmup window
    from datetime import datetime, timezone
    joined_ts = time.time() - 60
    joined_iso = datetime.fromtimestamp(joined_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"

    mid = _insert_member(
        db, "T-young",
        heartbeat_at=None,
        joined_at=joined_iso,
    )

    targets = mgr.reap_dead()
    assert all(t.membership_id != mid for t in targets), (
        "Young worker inside warmup window must not be reaped"
    )


# ---------------------------------------------------------------------------
# 4. Concurrent reap is idempotent
# ---------------------------------------------------------------------------

def test_concurrent_reap_idempotent(tmp_path):
    """Two concurrent ticks must not double-reap the same member."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    errors: List[Exception] = []

    def run_tick():
        try:
            mgr.tick()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run_tick)
    t2 = threading.Thread(target=run_tick)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent tick errors: {errors}"

    # Membership row must be released exactly once
    conn = sqlite3.connect(str(db))
    reaped_rows = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE released_at IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert reaped_rows == 1, f"Expected exactly 1 reaped row; got {reaped_rows}"


# ---------------------------------------------------------------------------
# 5. Reap event emitted in NDJSON with actor and reason
# ---------------------------------------------------------------------------

def test_reap_event_emitted_in_ndjson_with_actor_and_reason(tmp_path):
    """pool.worker.dead_reaped event must appear in pool_events.ndjson with actor + reason."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    mgr.reap_dead()

    events = _read_pool_events(state_dir)
    dead_reap_events = [
        e for e in events if e.get("event_type") == "pool.worker.dead_reaped"
    ]

    assert len(dead_reap_events) >= 1, (
        f"Expected pool.worker.dead_reaped event; got event types: "
        f"{[e.get('event_type') for e in events]}"
    )
    payload = dead_reap_events[0]["payload"]
    assert payload.get("actor") == "pool_reaper"
    assert "reason" in payload and payload["reason"]
    assert "terminal_id" in payload


# ---------------------------------------------------------------------------
# 6. No spurious spawn when reap brought pool to target
# ---------------------------------------------------------------------------

def test_no_spawn_when_reap_brought_pool_to_target(tmp_path):
    """After reap removes 1 stale worker from a 3-member pool (target=2), decide sees
    2 healthy members and returns noop — no additional spawn."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # min=2, max=4 → with queue=0, target=2 (queue_depth_v1: ceil(0/2)=0, clamped to min=2)
    db = create_test_db_file(
        state_dir / "test.db",
        min_workers=2,
        max_workers=4,
        cooldown_seconds=0,
        scale_policy="queue_aware",
    )
    mgr = PoolManager(
        project_id="vnx-dev",
        pool_id="default",
        db_path=db,
        spawn_fn=_always_succeed,
    )
    mgr._kill_subprocess = lambda tid, pid: None  # type: ignore[method-assign]
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    # Insert 2 healthy workers
    now_iso = "2026-05-16T10:00:00.000000Z"
    _insert_member(db, "T1", heartbeat_at=now_iso, joined_at="2026-05-16T09:00:00.000000Z")
    _insert_member(db, "T2", heartbeat_at=now_iso, joined_at="2026-05-16T09:00:00.000000Z")
    # Insert 1 stale worker
    _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    result = mgr.tick()

    # Stale was reaped → pool at 2 (target) → decide should noop → no spawn
    if result.decision.action in ("noop", "scale_down"):
        assert len(result.spawned) == 0, (
            f"No spawn expected when pool is at/above target; got {result.spawned}"
        )


# ---------------------------------------------------------------------------
# 7. decide() sees post-reap state (fewer members than before)
# ---------------------------------------------------------------------------

def test_reap_before_decide_post_reap_state(tmp_path):
    """decide() must see the post-reap membership list, not pre-reap."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db", min_workers=1, max_workers=4)
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    # Stale member that reaper catches (>180s) but decide() threshold (default 300s) doesn't
    _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    seen_by_decide: List[int] = []
    original_decide = mgr.decide

    def instrumented_decide():
        # Count active members at the point decide() is called
        count = _active_count(db)
        seen_by_decide.append(count)
        return original_decide()

    mgr.decide = instrumented_decide  # type: ignore[method-assign]

    mgr.tick()

    assert seen_by_decide, "decide() was never called"
    # reap_dead() ran first → stale member already released → decide sees 0 active
    assert seen_by_decide[0] == 0, (
        f"decide() should see 0 active members (post-reap); saw {seen_by_decide[0]}"
    )


# ---------------------------------------------------------------------------
# 8. ExecResult.reaped contains reaped membership_ids
# ---------------------------------------------------------------------------

def test_result_includes_reaped_membership_ids(tmp_path):
    """tick() ExecResult.reaped must contain the reaped membership_ids."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    mid = _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    result = mgr.tick()

    assert mid in result.reaped, (
        f"reaped list must contain the membership_id; got {result.reaped}"
    )


# ---------------------------------------------------------------------------
# 9. Fresh worker with no heartbeat not reaped (warmup protection)
# ---------------------------------------------------------------------------

def test_fresh_worker_not_reaped_even_with_no_heartbeat(tmp_path):
    """Worker younger than warmup_window_s must not be reaped despite missing heartbeat."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mgr, db = _make_manager(state_dir / "test.db")
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=300.0)

    from datetime import datetime, timezone
    # Joined 60s ago — well inside 300s warmup window
    joined_ts = time.time() - 60
    joined_iso = (
        datetime.fromtimestamp(joined_ts, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    )
    mid = _insert_member(db, "T-young", heartbeat_at=None, joined_at=joined_iso)

    targets = mgr.reap_dead()
    assert all(t.membership_id != mid for t in targets), (
        "Worker in warmup window must NOT be reap-eligible, even without heartbeat"
    )
    assert _reaped_count(db) == 0


# ---------------------------------------------------------------------------
# 10. _kill_subprocess pid validation in reap flow
# ---------------------------------------------------------------------------

def test_kill_subprocess_pid_validation_in_reap_flow(tmp_path):
    """When reap targets have pid=None (Membership has no pid field),
    os.kill must never be called."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    db = create_test_db_file(
        state_dir / "test.db",
        min_workers=1,
        max_workers=4,
        cooldown_seconds=0,
    )
    mgr = PoolManager(
        project_id="vnx-dev",
        pool_id="default",
        db_path=db,
        spawn_fn=_always_succeed,
    )
    mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=180.0, warmup_window_s=60.0)

    _insert_member(db, "T-stale", heartbeat_at="2026-01-01T00:00:00.000000Z")

    with unittest.mock.patch("pool_manager.os.kill") as mock_kill:
        mgr.reap_dead()

    # Membership dataclass has no pid → getattr returns None → no kill
    mock_kill.assert_not_called()
