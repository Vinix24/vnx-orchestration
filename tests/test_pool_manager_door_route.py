"""test_pool_manager_door_route.py — the pool starts its worker through the door (T-D0).

Covers the ADR-042 besluit 6 amendment of 2026-10-27:
  * a pool tick with the default environment starts ONE ``pool_worker_runner`` and ZERO
    ``subprocess_dispatch`` workers (the pool route is the single-entry door);
  * ``VNX_SINGLE_ENTRY_DISPATCH=0`` keeps the legacy ``subprocess_dispatch`` route reachable
    (the hard-parked rollback net);
  * a running pool worker renews the ``terminal_leases`` row of the terminal it serves
    (``last_heartbeat_at`` advances) and releases it at its end.

The spawn tests patch ``subprocess.Popen`` at the process boundary only — the command the
pool builds and the branch that builds it stay the real code. The lease test runs against a
real file-backed SQLite store in ``tmp_path``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

import pool_worker_runner  # noqa: E402
from pool_manager import PoolManager  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402

_DOOR_FLAG = "VNX_SINGLE_ENTRY_DISPATCH"
_LEGACY_FLAG = "VNX_DISPATCH_LEGACY"
_CONSUMER_FLAG = "VNX_POOL_TASK_CONSUMER"
_HEARTBEAT_ENV = "VNX_POOL_LEASE_HEARTBEAT_SECONDS"


def _env_without(*names: str) -> dict:
    """The ambient environment minus the routing flags, so the default is really unset."""
    return {k: v for k, v in os.environ.items() if k not in names}


def _pool_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_test_db_file(
        db_path, min_workers=1, max_workers=4, target_workers=1, cooldown_seconds=0,
    )


def _tick(tmp_path: Path, env: dict):
    """One real pool tick with Popen patched at the process boundary."""
    wt = tmp_path / "worktrees" / "worker"
    wt.mkdir(parents=True, exist_ok=True)
    proc = MagicMock()
    proc.pid = os.getpid()  # a live PID: the post-spawn os.kill probe must pass
    db_path = _pool_db(tmp_path)
    with patch("pool_worktree_manager.create_worker_worktree", return_value=wt), \
            patch.object(sys.modules["pool_manager"].subprocess, "Popen", return_value=proc) as popen, \
            patch.dict(os.environ, env, clear=True):
        result = PoolManager("vnx-dev", "default", db_path).tick()
    return result, popen


def _commands(popen) -> list:
    return [" ".join(call[0][0]) for call in popen.call_args_list]


def _heartbeat(db_path: Path, terminal_id: str, project_id: str = "proj"):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT last_heartbeat_at FROM terminal_leases "
            "WHERE terminal_id = ? AND project_id = ?",
            (terminal_id, project_id),
        ).fetchone()
    finally:
        conn.close()
    return datetime.fromisoformat(row[0].rstrip("Z")).replace(tzinfo=timezone.utc)


def _lease_row(db_path: Path, terminal_id: str, project_id: str = "proj"):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT state, worker_pid, lease_token FROM terminal_leases "
            "WHERE terminal_id = ? AND project_id = ?",
            (terminal_id, project_id),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Default tick: the door route
# ---------------------------------------------------------------------------

def test_default_tick_starts_pool_worker_runner_once_and_never_subprocess_dispatch(tmp_path):
    """Default env → exactly one ``pool_worker_runner`` Popen, zero ``subprocess_dispatch``."""
    env = _env_without(_DOOR_FLAG, _LEGACY_FLAG, _CONSUMER_FLAG)
    result, popen = _tick(tmp_path, env)

    assert result.spawned, f"expected one spawn, got errors={result.errors}"
    assert popen.call_count == 1, _commands(popen)
    cmds = _commands(popen)
    assert "scripts.lib.pool_worker_runner" in cmds[0]
    assert not any("scripts.lib.subprocess_dispatch" in c for c in cmds), cmds


# ---------------------------------------------------------------------------
# 2. Rollback: VNX_SINGLE_ENTRY_DISPATCH=0 keeps the legacy worker
# ---------------------------------------------------------------------------

def test_rollback_flag_restores_subprocess_dispatch_route(tmp_path):
    """VNX_SINGLE_ENTRY_DISPATCH=0 → the legacy ``subprocess_dispatch`` worker, and only it."""
    env = _env_without(_LEGACY_FLAG, _CONSUMER_FLAG)
    env[_DOOR_FLAG] = "0"
    result, popen = _tick(tmp_path, env)

    assert result.spawned, f"expected one spawn, got errors={result.errors}"
    assert popen.call_count == 1, _commands(popen)
    cmds = _commands(popen)
    assert "scripts.lib.subprocess_dispatch" in cmds[0]
    assert not any("scripts.lib.pool_worker_runner" in c for c in cmds), cmds


# ---------------------------------------------------------------------------
# 3. Lease ownership lives in the pool worker
# ---------------------------------------------------------------------------

def _write_bundle(dispatch_dir: Path, dispatch_id: str) -> None:
    bundle = dispatch_dir / dispatch_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "prompt.txt").write_text("# worker instruction\n", encoding="utf-8")
    (bundle / "bundle.json").write_text(
        '{"dispatch_id": "%s", "target_profile": {"provider": "claude"}, "gate": ""}'
        % dispatch_id,
        encoding="utf-8",
    )


def _insert_dispatch(db_path: Path, dispatch_id: str, project_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, 'queued')",
            (dispatch_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_running_pool_worker_renews_its_lease_and_releases_it_at_the_end(tmp_path, monkeypatch):
    """A worker that runs renews ``last_heartbeat_at`` while it works, then releases the row."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = create_test_db_file(
        state_dir / "runtime_coordination.db", project_id="proj",
    )
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    _insert_dispatch(db_path, "d-lease", "proj")
    _write_bundle(dispatch_dir, "d-lease")

    # The pool manager writes this row right after spawn; the worker inherits it.
    PoolStateRepository(db_path, "proj").add_or_refresh_pool_lease("T1", os.getpid(), time.time())
    before = _heartbeat(db_path, "T1")

    monkeypatch.setenv(_HEARTBEAT_ENV, "0.02")
    monkeypatch.setattr(
        pool_worker_runner, "claim_next_queued_dispatch",
        lambda conn, terminal_id, project_id: "d-lease",
    )
    during = []

    def _slow_delivery(*_args, **_kwargs) -> int:
        time.sleep(0.15)
        during.append(_heartbeat(db_path, "T1"))
        return pool_worker_runner.EXIT_OK

    monkeypatch.setattr(pool_worker_runner, "_deliver_claude", _slow_delivery)

    rc = pool_worker_runner.main([
        "--terminal-id", "T1",
        "--project-id", "proj",
        "--state-dir", str(state_dir),
        "--dispatch-dir", str(dispatch_dir),
    ])

    assert rc == pool_worker_runner.EXIT_OK
    assert during, "delivery was never reached"
    assert during[0] > before, (
        f"a running worker must renew last_heartbeat_at; before={before} during={during[0]}"
    )
    state, worker_pid, lease_token = _lease_row(db_path, "T1")
    assert state == "released", f"worker must release its lease at the end, got state={state!r}"
    assert worker_pid is None
    assert lease_token == ""


def test_non_numeric_heartbeat_interval_falls_back_to_the_default(monkeypatch):
    """A malformed interval is not a crash and not a silent zero delay: the 15s default wins."""
    monkeypatch.setenv(_HEARTBEAT_ENV, "not-a-number")
    assert pool_worker_runner._lease_heartbeat_interval() == 15.0
    monkeypatch.setenv(_HEARTBEAT_ENV, "0")
    assert pool_worker_runner._lease_heartbeat_interval() == 15.0
    monkeypatch.setenv(_HEARTBEAT_ENV, "2.5")
    assert pool_worker_runner._lease_heartbeat_interval() == 2.5
