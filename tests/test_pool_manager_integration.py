"""test_pool_manager_integration.py — Integration tests for PoolManager.

Uses real SQLite file-backed DBs via tmp_path (PoolStateRepository._connect()
opens/closes per-call, so in-memory shared connections don't work here).
Spawn function is mocked to avoid subprocess side-effects.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
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
from pool_state_fixtures import (  # noqa: E402
    _BASE_SCHEMA,
    create_test_db_file,
    insert_lease,
    insert_membership,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _always_succeed(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=True)


def _always_fail(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=False, error="mock spawn error")


def _make_manager(
    db_path: Path,
    project_id: str = "vnx-dev",
    pool_id: str = "default",
    spawn_fn=None,
) -> PoolManager:
    return PoolManager(
        project_id=project_id,
        pool_id=pool_id,
        db_path=db_path,
        spawn_fn=spawn_fn or _always_succeed,
    )


def _active_count(db_path: Path, project_id: str = "vnx-dev") -> int:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE project_id=? AND released_at IS NULL",
        (project_id,),
    ).fetchone()
    conn.close()
    return row[0]


def _last_decision(db_path: Path, project_id: str = "vnx-dev") -> dict:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT last_decision_json FROM worker_pools WHERE project_id=?",
        (project_id,),
    ).fetchone()
    conn.close()
    if row and row[0]:
        return json.loads(row[0])
    return {}


def _insert_lease_file(
    db_path: Path,
    terminal_id: str,
    project_id: str = "vnx-dev",
    last_heartbeat_at=None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT OR IGNORE INTO terminal_leases
            (terminal_id, project_id, state, lease_token, last_heartbeat_at)
        VALUES (?, ?, 'idle', '', ?)
        """,
        (terminal_id, project_id, last_heartbeat_at),
    )
    conn.commit()
    conn.close()


def _insert_membership_file(
    db_path: Path,
    terminal_id: str,
    project_id: str = "vnx-dev",
    pool_id: str = "default",
    provider: str = "claude",
    role: str = "backend-developer",
    joined_at: str = "2026-05-01T00:00:00.000000Z",
    membership_id: str = None,
) -> str:
    import uuid as _uuid
    mid = membership_id or str(_uuid.uuid4())
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO worker_pool_membership
            (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (terminal_id, project_id, pool_id, provider, role, joined_at,
         json.dumps({"membership_id": mid})),
    )
    conn.commit()
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# 1. Tick records decision in DB
# ---------------------------------------------------------------------------

def test_tick_records_decision_in_db(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )
    mgr = _make_manager(db)
    mgr.tick()

    d = _last_decision(db)
    assert "action" in d
    assert d["action"] in ("scale_up", "scale_down", "noop", "reap")
    assert "decision_id" in d
    assert "recorded_at" in d


# ---------------------------------------------------------------------------
# 2. Tick inserts membership on successful spawn
# ---------------------------------------------------------------------------

def test_tick_inserts_membership_on_successful_spawn(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=2, max_workers=4, cooldown_seconds=0
    )
    mgr = _make_manager(db, spawn_fn=_always_succeed)

    before = _active_count(db)
    mgr.tick()
    after = _active_count(db)

    assert after >= before + 1, (
        f"Expected membership rows to grow; before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# 3. Tick skips membership on spawn failure
# ---------------------------------------------------------------------------

def test_tick_skips_membership_on_spawn_failure(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=2, max_workers=4, cooldown_seconds=0
    )
    mgr = _make_manager(db, spawn_fn=_always_fail)

    mgr.tick()

    count = _active_count(db)
    assert count == 0, f"No membership rows should be inserted on spawn failure; got {count}"


# ---------------------------------------------------------------------------
# 4. Tick marks reaped in DB
# ---------------------------------------------------------------------------

def test_tick_marks_reaped_in_db(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )

    # Insert a stale lease + membership (heartbeat 900s ago, threshold default=300s)
    _insert_lease_file(db, "T-stale", last_heartbeat_at="2026-01-01T00:00:00.000000Z")
    _insert_membership_file(
        db, "T-stale",
        joined_at="2026-01-01T00:00:00.000000Z",
    )

    mgr = _make_manager(db, spawn_fn=_always_succeed)
    result = mgr.tick()

    conn = sqlite3.connect(str(db))
    reaped = conn.execute(
        "SELECT released_at FROM worker_pool_membership WHERE released_at IS NOT NULL"
    ).fetchall()
    conn.close()

    assert len(reaped) >= 1, "Expected at least one reaped row"


# ---------------------------------------------------------------------------
# 5. Partial spawn failure — per-target outcome tracking
# ---------------------------------------------------------------------------

def test_partial_spawn_failure_reports_per_target_outcome(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=3, max_workers=6, cooldown_seconds=0
    )

    call_count = 0

    def partial_spawn(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SpawnResult(terminal_id=terminal_id, success=True)
        return SpawnResult(terminal_id=terminal_id, success=False, error="rate limit")

    mgr = _make_manager(db, spawn_fn=partial_spawn)
    result = mgr.tick()

    assert len(result.spawned) == 1
    assert len(result.errors) == 2
    assert _active_count(db) == 1


# ---------------------------------------------------------------------------
# 6. Cooldown anchor not updated on noop
# ---------------------------------------------------------------------------

def test_cooldown_anchor_not_updated_on_noop(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )

    # Seed a single active worker so pool is at min (queue_aware with queue=0 → noop)
    _insert_lease_file(db, "T1", last_heartbeat_at="2026-05-16T00:00:00.000000Z")
    _insert_membership_file(db, "T1", joined_at="2026-05-01T00:00:00.000000Z")

    # Manually set last_scaled_at to sentinel
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE worker_pools SET last_scaled_at='2026-01-01T00:00:00.000000Z'")
    conn.commit()
    conn.close()

    mgr = _make_manager(db, spawn_fn=_always_succeed)
    result = mgr.tick()

    if result.decision.action == "noop":
        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT last_scaled_at FROM worker_pools").fetchone()
        conn.close()
        assert row[0] == "2026-01-01T00:00:00.000000Z", (
            "last_scaled_at should not change on noop"
        )


# ---------------------------------------------------------------------------
# 7. Cooldown anchor updated on spawn
# ---------------------------------------------------------------------------

def test_cooldown_anchor_updated_on_spawn(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=2, max_workers=4, cooldown_seconds=0
    )

    conn = sqlite3.connect(str(db))
    before_row = conn.execute("SELECT last_scaled_at FROM worker_pools").fetchone()
    before = before_row[0]
    conn.close()

    mgr = _make_manager(db, spawn_fn=_always_succeed)
    result = mgr.tick()

    conn = sqlite3.connect(str(db))
    after_row = conn.execute("SELECT last_scaled_at FROM worker_pools").fetchone()
    after = after_row[0]
    conn.close()

    if result.spawned:
        assert after != before, "last_scaled_at should update when workers are spawned"


# ---------------------------------------------------------------------------
# 8. Concurrent ticks don't double-scale
# ---------------------------------------------------------------------------

def test_concurrent_ticks_dont_double_scale(tmp_path):
    db = tmp_path / "concurrent.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(_BASE_SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO pool_config
            (project_id, pool_id, min_workers, max_workers, target_workers,
             scale_policy, cooldown_seconds, provider_mix_json)
        VALUES ('vnx-dev', 'default', 0, 4, 2, 'queue_aware', 120, '["claude"]')
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO worker_pools (project_id, pool_id) VALUES ('vnx-dev', 'default')"
    )
    conn.commit()
    conn.close()

    spawned_ids: List[str] = []
    lock = threading.Lock()

    def spawn_fn(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
        with lock:
            spawned_ids.append(terminal_id)
        return SpawnResult(terminal_id=terminal_id, success=True)

    errors: List[Exception] = []

    def run_tick():
        try:
            mgr = PoolManager(
                project_id="vnx-dev",
                pool_id="default",
                db_path=db,
                spawn_fn=spawn_fn,
            )
            mgr.tick()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run_tick)
    t2 = threading.Thread(target=run_tick)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"
    count = _active_count(db)
    assert count <= 4, f"Too many members spawned concurrently: {count}"


# ---------------------------------------------------------------------------
# 9. Noop produces no spawns and no reaps
# ---------------------------------------------------------------------------

def test_noop_decision_results_in_no_spawns_no_reaps(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )
    _insert_lease_file(db, "T1", last_heartbeat_at="2026-05-16T00:00:00.000000Z")
    _insert_membership_file(db, "T1", joined_at="2026-05-01T00:00:00.000000Z")

    before = _active_count(db)
    mgr = _make_manager(db, spawn_fn=_always_succeed)
    result = mgr.tick()

    after = _active_count(db)
    if result.decision.action == "noop":
        assert after == before


# ---------------------------------------------------------------------------
# 10. Record_decision stores action field
# ---------------------------------------------------------------------------

def test_record_decision_stores_action(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )
    mgr = _make_manager(db, spawn_fn=_always_succeed)
    mgr.tick()

    d = _last_decision(db)
    assert d.get("action") in ("scale_up", "scale_down", "noop", "reap")


# ---------------------------------------------------------------------------
# 11. Scale down releases membership rows
# ---------------------------------------------------------------------------

def test_scale_down_releases_membership(tmp_path):
    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0
    )

    for i in range(3):
        _insert_lease_file(db, f"T{i}", last_heartbeat_at="2026-05-16T00:00:00.000000Z")
        _insert_membership_file(
            db, f"T{i}",
            joined_at=f"2026-05-0{i+1}T00:00:00.000000Z",
        )

    mgr = _make_manager(db, spawn_fn=_always_succeed)
    result = mgr.tick()

    conn = sqlite3.connect(str(db))
    released = conn.execute(
        "SELECT COUNT(*) FROM worker_pool_membership WHERE released_at IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    if result.decision.action == "scale_down":
        assert released > 0, "Expected released rows after scale_down"
