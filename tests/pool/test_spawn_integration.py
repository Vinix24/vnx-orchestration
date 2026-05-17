"""test_spawn_integration.py — End-to-end integration test for pool spawn flow.

Validates the full lifecycle: worktree create → Popen → PID capture →
membership record → reaper PID validation → worktree cleanup.

All subprocess calls (git worktree, claude CLI) are mocked at the boundary,
but the internal wiring between pool_manager, pool_worktree_manager,
pool_state_repo, and pool_reaper runs unpatched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_manager import (
    ExecResult,
    PoolManager,
    SpawnResult,
    _spawn_via_provider_dispatch,
)
from pool_reaper import ReapConfig, ReapTarget
from pool_state_repo import PoolStateRepository
from pool_state_fixtures import create_test_db_file


def _setup_db(tmp_path: Path, min_workers: int = 0, max_workers: int = 4) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    return create_test_db_file(
        db_path,
        min_workers=min_workers,
        max_workers=max_workers,
        target_workers=max(min_workers, 1),
    )


class TestSpawnIntegrationFullFlow:
    """End-to-end: worktree create → Popen → PID in DB → reaper validates PID."""

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_full_spawn_flow_worktree_to_receipt(self, mock_kill, mock_popen, mock_wt, tmp_path):
        wt_path = tmp_path / "worktrees" / "pool-T1"
        wt_path.mkdir(parents=True)
        mock_wt.return_value = wt_path

        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_popen.return_value = mock_proc

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is True
        assert result.pid == 88888
        assert result.terminal_id == "T1"

        mock_wt.assert_called_once_with("T1")
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(wt_path)
        assert call_kwargs["start_new_session"] is True

        cmd = mock_popen.call_args[0][0]
        assert "--terminal-id" in cmd
        assert "--dispatch-id" in cmd
        assert "--role" in cmd
        assert "backend-developer" in cmd

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_pid_persists_through_membership_and_reaper(
        self, mock_kill, mock_popen, mock_wt, tmp_path
    ):
        mock_wt.return_value = tmp_path / "worktrees" / "pool-T1"
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_popen.return_value = mock_proc

        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-int-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        mgr = PoolManager(
            "vnx-dev", "default", db_path,
            spawn_fn=_spawn_via_provider_dispatch,
        )
        result = mgr.tick()

        assert len(result.spawned) >= 1

        repo = PoolStateRepository(db_path, "vnx-dev")
        members = repo.list_members("default")
        assert len(members) >= 1
        spawned_member = next(m for m in members if m.pid == 55555)
        assert spawned_member.provider == "claude"

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_reaper_uses_pid_from_spawn_for_kill(
        self, mock_kill, mock_popen, mock_wt, tmp_path
    ):
        mock_wt.return_value = tmp_path / "worktrees" / "pool-T1"
        mock_proc = MagicMock()
        mock_proc.pid = 44444
        mock_popen.return_value = mock_proc

        db_path = _setup_db(tmp_path)
        repo = PoolStateRepository(db_path, "vnx-dev")
        repo.add_member("default", "T1", "claude", "backend-developer", 100.0, pid=44444)

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr.reap_config = ReapConfig(
            heartbeat_stale_threshold_s=10.0,
            warmup_window_s=5.0,
        )

        with patch.object(mgr, "_kill_subprocess") as mock_kill_sub:
            with patch("pool_worktree_manager.reap_worker_worktree"):
                reaped = mgr.reap_dead()

        assert len(reaped) >= 1
        kill_call = mock_kill_sub.call_args_list[0]
        assert kill_call[0][0] == "T1"
        assert kill_call[0][1] == 44444

    @patch("pool_worktree_manager.reap_worker_worktree")
    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_full_lifecycle_spawn_and_reap(
        self, mock_kill, mock_popen, mock_wt, mock_reap_wt, tmp_path
    ):
        mock_wt.return_value = tmp_path / "worktrees" / "pool-T1"
        mock_proc = MagicMock()
        mock_proc.pid = 33333
        mock_popen.return_value = mock_proc

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-lifecycle-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        mgr = PoolManager(
            "vnx-dev", "default", db_path,
            spawn_fn=_spawn_via_provider_dispatch,
        )
        spawn_result = mgr.tick()
        assert len(spawn_result.spawned) >= 1

        repo = PoolStateRepository(db_path, "vnx-dev")
        members = repo.list_members("default")
        assert len(members) >= 1
        member = members[0]
        assert member.pid == 33333

        mgr.reap_config = ReapConfig(
            heartbeat_stale_threshold_s=0.001,
            warmup_window_s=0.0,
        )

        mock_kill.side_effect = [None, ProcessLookupError]

        with patch("pool_manager.time.sleep"):
            reaped = mgr.reap_dead()

        assert len(reaped) >= 1
        mock_reap_wt.assert_called()

    def test_worktree_failure_does_not_leave_orphan_membership(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-orphan-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        call_count = {"n": 0}

        def spawn_fn_worktree_fails(project_id, pool_id, terminal_id, provider, role):
            call_count["n"] += 1
            return SpawnResult(
                terminal_id=terminal_id,
                success=False,
                error="worktree creation failed: git error",
            )

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=spawn_fn_worktree_fails)
        result = mgr.tick()

        assert len(result.spawned) == 0
        assert len(result.errors) >= 1
        assert "worktree creation failed" in result.errors[0]

        repo = PoolStateRepository(db_path, "vnx-dev")
        members = repo.list_members("default")
        assert len(members) == 0
