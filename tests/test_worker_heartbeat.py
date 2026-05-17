"""test_worker_heartbeat.py — Worker heartbeat + PID validation tests.

Covers:
- Pool heartbeat loop updates terminal_leases.last_heartbeat_at
- store_worker_pid writes PID to terminal_leases
- update_heartbeat_by_terminal updates last_heartbeat_at by terminal_id
- PID validation: is_pid_alive returns False for dead PIDs
- identify_dead_pid_targets reaps members with dead PIDs
- identify_dead_pid_targets skips members with alive PIDs
- PID <= 0 safety: never probed via os.kill

Wave 6 PR-6.5c — Worker heartbeat + PID validation in reaper.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import unittest.mock
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_decision_engine import Membership
from pool_reaper import identify_dead_pid_targets, is_pid_alive
from pool_state_repo import PoolStateRepository
from pool_state_fixtures import create_test_db_file, insert_lease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(db_path: Path, project_id: str = "vnx-dev") -> PoolStateRepository:
    return PoolStateRepository(db_path, project_id)


def _setup_db(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    return create_test_db_file(tmp_path / "state" / "runtime_coordination.db")


def _read_heartbeat(db_path: Path, terminal_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_heartbeat_at FROM terminal_leases WHERE terminal_id = ?",
        (terminal_id,),
    ).fetchone()
    conn.close()
    return row["last_heartbeat_at"] if row else None


def _read_pid(db_path: Path, terminal_id: str) -> int | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT worker_pid FROM terminal_leases WHERE terminal_id = ?",
        (terminal_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return int(row["worker_pid"]) if row["worker_pid"] is not None else None


def _member_with_pid(
    membership_id: str = "m-001",
    terminal_id: str = "T1",
    pid: int | None = None,
    status: str = "active",
    joined_at: float = 0.0,
    last_heartbeat: float | None = None,
) -> Membership:
    return Membership(
        membership_id=membership_id,
        terminal_id=terminal_id,
        provider="claude",
        pool_role="backend-developer",
        status=status,
        joined_at=joined_at,
        last_heartbeat=last_heartbeat,
        pid=pid,
    )


# ---------------------------------------------------------------------------
# 1. store_worker_pid writes PID to terminal_leases
# ---------------------------------------------------------------------------

def test_store_worker_pid(tmp_path):
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
        "VALUES ('T1', 'vnx-dev', 'active', 'tok-1')",
    )
    conn.commit()
    conn.close()

    repo = _make_repo(db)
    repo.store_worker_pid("T1", 12345)

    stored_pid = _read_pid(db, "T1")
    assert stored_pid == 12345


# ---------------------------------------------------------------------------
# 2. update_heartbeat_by_terminal updates last_heartbeat_at
# ---------------------------------------------------------------------------

def test_update_heartbeat_by_terminal(tmp_path):
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
        "VALUES ('T1', 'vnx-dev', 'active', 'tok-1')",
    )
    conn.commit()
    conn.close()

    repo = _make_repo(db)
    now = time.time()
    repo.update_heartbeat_by_terminal("T1", now)

    hb = _read_heartbeat(db, "T1")
    assert hb is not None
    assert "T" not in hb or "Z" in hb  # ISO format


def test_update_heartbeat_by_terminal_overwrites_previous(tmp_path):
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token, last_heartbeat_at) "
        "VALUES ('T1', 'vnx-dev', 'active', 'tok-1', '2020-01-01T00:00:00.000000Z')",
    )
    conn.commit()
    conn.close()

    repo = _make_repo(db)
    now = time.time()
    repo.update_heartbeat_by_terminal("T1", now)

    hb = _read_heartbeat(db, "T1")
    assert hb is not None
    assert "2020" not in hb


# ---------------------------------------------------------------------------
# 3. Pool heartbeat loop integration
# ---------------------------------------------------------------------------

def test_pool_heartbeat_loop_updates_heartbeat(tmp_path):
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token) "
        "VALUES ('T1', 'vnx-dev', 'active', 'tok-1')",
    )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(_LIB_DIR))
    from subprocess_dispatch import _pool_heartbeat_loop

    stop = threading.Event()
    t = threading.Thread(
        target=_pool_heartbeat_loop,
        args=("T1", "vnx-dev", db, stop),
        kwargs={"interval": 0.1},
        daemon=True,
    )
    t.start()
    time.sleep(0.35)
    stop.set()
    t.join(timeout=2)

    hb = _read_heartbeat(db, "T1")
    assert hb is not None


# ---------------------------------------------------------------------------
# 4. is_pid_alive
# ---------------------------------------------------------------------------

def test_is_pid_alive_returns_true_for_self():
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_returns_false_for_none():
    assert is_pid_alive(None) is False


def test_is_pid_alive_returns_false_for_zero():
    assert is_pid_alive(0) is False


def test_is_pid_alive_returns_false_for_negative():
    assert is_pid_alive(-1) is False


def test_is_pid_alive_returns_false_for_dead_pid():
    with unittest.mock.patch("pool_reaper.os.kill", side_effect=ProcessLookupError):
        assert is_pid_alive(99999) is False


def test_is_pid_alive_returns_true_on_permission_error():
    with unittest.mock.patch("pool_reaper.os.kill", side_effect=PermissionError):
        assert is_pid_alive(99999) is True


# ---------------------------------------------------------------------------
# 5. identify_dead_pid_targets
# ---------------------------------------------------------------------------

def test_dead_pid_produces_reap_target():
    m = _member_with_pid(pid=99999)
    with unittest.mock.patch("pool_reaper.os.kill", side_effect=ProcessLookupError):
        targets = identify_dead_pid_targets([m])
    assert len(targets) == 1
    assert targets[0].membership_id == "m-001"
    assert "pid_dead" in targets[0].reason


def test_alive_pid_not_reaped():
    m = _member_with_pid(pid=os.getpid())
    targets = identify_dead_pid_targets([m])
    assert targets == []


def test_no_pid_member_skipped():
    m = _member_with_pid(pid=None)
    targets = identify_dead_pid_targets([m])
    assert targets == []


def test_zero_pid_member_skipped():
    m = _member_with_pid(pid=0)
    targets = identify_dead_pid_targets([m])
    assert targets == []


def test_negative_pid_member_skipped():
    m = _member_with_pid(pid=-1)
    targets = identify_dead_pid_targets([m])
    assert targets == []


def test_non_active_member_with_dead_pid_skipped():
    m = _member_with_pid(pid=99999, status="draining")
    with unittest.mock.patch("pool_reaper.os.kill", side_effect=ProcessLookupError):
        targets = identify_dead_pid_targets([m])
    assert targets == []


def test_mixed_alive_and_dead_pids():
    alive = _member_with_pid(membership_id="alive", pid=os.getpid())
    dead = _member_with_pid(membership_id="dead", pid=99999)

    def selective_kill(pid, sig):
        if pid == 99999:
            raise ProcessLookupError
        return None

    with unittest.mock.patch("pool_reaper.os.kill", side_effect=selective_kill):
        targets = identify_dead_pid_targets([alive, dead])
    assert len(targets) == 1
    assert targets[0].membership_id == "dead"


# ---------------------------------------------------------------------------
# 6. list_members includes pid from terminal_leases
# ---------------------------------------------------------------------------

def test_list_members_includes_pid(tmp_path):
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token, worker_pid) "
        "VALUES ('T-pid-01', 'vnx-dev', 'active', 'tok-1', 42)",
    )
    import json
    conn.execute(
        "INSERT INTO worker_pool_membership "
        "(terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json) "
        "VALUES ('T-pid-01', 'vnx-dev', 'default', 'claude', 'backend-developer', "
        "'2026-01-01T00:00:00.000000Z', ?)",
        (json.dumps({"membership_id": "m-pid-01"}),),
    )
    conn.commit()
    conn.close()

    repo = _make_repo(db)
    members = repo.list_members("default")
    assert len(members) == 1
    assert members[0].pid == 42


def test_list_members_pid_none_when_not_set(tmp_path):
    db = _setup_db(tmp_path)
    repo = _make_repo(db)
    now = time.time()
    repo.add_member("default", "T-nopid-01", "claude", "backend-developer", now)

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT OR IGNORE INTO terminal_leases (terminal_id, project_id, state, lease_token) "
        "VALUES ('T-nopid-01', 'vnx-dev', 'active', 'tok-1')",
    )
    conn.commit()
    conn.close()

    members = repo.list_members("default")
    assert len(members) == 1
    assert members[0].pid is None


# ---------------------------------------------------------------------------
# 7. Reaper integration: dead PID triggers reap via PoolManager
# ---------------------------------------------------------------------------

def test_pool_manager_reap_dead_catches_dead_pid(tmp_path):
    from pool_manager import PoolManager, SpawnResult

    db = _setup_db(tmp_path)

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, project_id, state, lease_token, worker_pid, last_heartbeat_at) "
        "VALUES ('T-dead', 'vnx-dev', 'active', 'tok-1', 99999, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),),
    )
    import json
    conn.execute(
        "INSERT INTO worker_pool_membership "
        "(terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json) "
        "VALUES ('T-dead', 'vnx-dev', 'default', 'claude', 'backend-developer', "
        "'2026-01-01T00:00:00.000000Z', ?)",
        (json.dumps({"membership_id": "m-dead-pid"}),),
    )
    conn.commit()
    conn.close()

    mgr = PoolManager(
        project_id="vnx-dev",
        pool_id="default",
        db_path=db,
        spawn_fn=lambda *a: SpawnResult(terminal_id=a[2], success=True),
    )

    with unittest.mock.patch("os.kill", side_effect=ProcessLookupError):
        reaped = mgr.reap_dead()

    assert len(reaped) == 1
    assert reaped[0].membership_id == "m-dead-pid"
    assert "pid_dead" in reaped[0].reason
