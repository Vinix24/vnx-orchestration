"""test_pool_consumer.py — VNX_POOL_TASK_CONSUMER flag integration tests (N-3).

Verifies:
- Flag on  → _spawn_via_provider_dispatch uses pool_worker_runner (not subprocess_dispatch)
- Flag off → unchanged generic "Pool worker" instruction (no regression)
- PoolManager.tick() with 6 queued dispatches + flag on spawns runner for each worker
- pool_id passed through to runner args
- Exact-match on '1': other values keep legacy path

ADR-007: project_id-scoped pool lease + dispatch match (FM-4).
ADR-018: pool sizing via tick (Rule 2); single-claim, no loop.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_manager import PoolManager, SpawnResult, _spawn_via_provider_dispatch  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402


def _setup_db(tmp_path: Path, min_workers: int = 0, max_workers: int = 6) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    return create_test_db_file(
        db_path,
        min_workers=min_workers,
        max_workers=max_workers,
        target_workers=max(min_workers, 1),
    )


def _enqueue(db_path: Path, count: int, project_id: str = "vnx-dev") -> None:
    conn = sqlite3.connect(str(db_path))
    for i in range(count):
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            (f"d-consumer-{i:03d}", project_id, "queued"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Unit — _spawn_via_provider_dispatch with consumer flag
# ---------------------------------------------------------------------------

class TestConsumerFlagOnSpawn:
    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_on_uses_pool_worker_runner(self, mock_kill, mock_popen, mock_wt):
        """VNX_POOL_TASK_CONSUMER=1 → cmd invokes pool_worker_runner, not subprocess_dispatch."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T1", "claude", "backend-developer"
            )

        assert result.success is True
        assert result.pid == 11111
        cmd = mock_popen.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "pool_worker_runner" in cmd_str
        assert "--terminal-id" in cmd
        assert "T1" in cmd
        assert "--project-id" in cmd
        assert "vnx-dev" in cmd
        # must NOT fall back to subprocess_dispatch or generic instruction
        assert "subprocess_dispatch" not in cmd_str
        assert "--instruction" not in cmd

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_on_passes_pool_id(self, mock_kill, mock_popen, mock_wt):
        """pool_id forwarded to pool_worker_runner via --pool-id."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 22222
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "batch-pool", "T2", "claude", "backend-developer"
            )

        assert result.success is True
        cmd = mock_popen.call_args[0][0]
        assert "--pool-id" in cmd
        pool_id_idx = cmd.index("--pool-id") + 1
        assert cmd[pool_id_idx] == "batch-pool"

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_off_keeps_generic_instruction(self, mock_kill, mock_popen, mock_wt):
        """Flag unset → subprocess_dispatch with generic 'Pool worker' instruction — no regression."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 33333
        mock_popen.return_value = mock_proc

        env = {k: v for k, v in os.environ.items() if k != "VNX_POOL_TASK_CONSUMER"}
        with patch.dict(os.environ, env, clear=True):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T3", "claude", "code-reviewer"
            )

        assert result.success is True
        cmd = mock_popen.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "subprocess_dispatch" in cmd_str
        assert "--instruction" in cmd
        instr_idx = cmd.index("--instruction") + 1
        assert "Pool worker T3" in cmd[instr_idx]
        assert "default" in cmd[instr_idx]

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_value_0_uses_generic_instruction(self, mock_kill, mock_popen, mock_wt):
        """VNX_POOL_TASK_CONSUMER=0 (not '1') keeps legacy path — exact string match."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 44444
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "0"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T4", "claude", "backend-developer"
            )

        assert result.success is True
        cmd = mock_popen.call_args[0][0]
        assert "--instruction" in cmd
        assert "Pool worker T4" in cmd[cmd.index("--instruction") + 1]

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_on_preserves_start_new_session(self, mock_kill, mock_popen, mock_wt):
        """Consumer mode still spawns detached (start_new_session=True)."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            _spawn_via_provider_dispatch("vnx-dev", "default", "T5", "claude", "backend-developer")

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_on_uses_worktree_cwd(self, mock_kill, mock_popen, mock_wt, tmp_path):
        """Consumer mode still creates a worktree and sets cwd to it."""
        wt = tmp_path / "worktrees" / "T6"
        wt.mkdir(parents=True)
        mock_wt.return_value = wt
        mock_proc = MagicMock()
        mock_proc.pid = 66666
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T6", "claude", "backend-developer"
            )

        assert result.success is True
        mock_wt.assert_called_once_with("T6")
        assert mock_popen.call_args[1]["cwd"] == str(wt)


# ---------------------------------------------------------------------------
# Negative-path — consumer mode propagates spawn failures unchanged
# ---------------------------------------------------------------------------

class TestConsumerFlagNegativePaths:
    @patch("pool_worktree_manager.create_worker_worktree")
    def test_flag_on_worktree_failure(self, mock_wt):
        """Worktree failure in consumer mode surfaces as SpawnResult(success=False)."""
        mock_wt.side_effect = RuntimeError("git worktree add failed")

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T1", "claude", "backend-developer"
            )

        assert result.success is False
        assert "worktree creation failed" in result.error

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_flag_on_process_dies_immediately(self, mock_kill, mock_popen, mock_wt):
        """Immediate exit in consumer mode surfaces as SpawnResult(success=False)."""
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 77777
        mock_popen.return_value = mock_proc
        mock_kill.side_effect = ProcessLookupError("No such process")

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T1", "claude", "backend-developer"
            )

        assert result.success is False
        assert "died immediately" in result.error


# ---------------------------------------------------------------------------
# Integration — PoolManager.tick() with consumer flag + 6 queued dispatches
# ---------------------------------------------------------------------------

class TestPoolManagerConsumerIntegration:
    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_tick_with_6_dispatches_spawns_runners(self, mock_kill, mock_popen, mock_wt, tmp_path):
        """6 queued dispatches + flag on → PoolManager.tick() spawns runner for each worker.

        ADR-018 Rule 2: single-claim, no loop; pool re-spawns on next tick.
        ADR-007 FM-4: project_id-scoped dispatch claim.
        queue_depth_v1 policy sizes pool to queue backlog each tick.
        """
        wt = tmp_path / "worktrees" / "worker"
        wt.mkdir(parents=True)
        mock_wt.return_value = wt
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_popen.return_value = mock_proc

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=6)
        _enqueue(db_path, 6)

        mgr = PoolManager("vnx-dev", "default", db_path)

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = mgr.tick()

        assert len(result.spawned) >= 1
        assert len(result.errors) == 0

        # Every Popen call must use pool_worker_runner, never subprocess_dispatch
        assert mock_popen.call_count >= 1
        for call in mock_popen.call_args_list:
            cmd = call[0][0]
            cmd_str = " ".join(cmd)
            assert "pool_worker_runner" in cmd_str, f"Expected pool_worker_runner in: {cmd_str}"
            assert "--terminal-id" in cmd
            assert "--project-id" in cmd
            assert "vnx-dev" in cmd
            assert "--pool-id" in cmd
            assert "--instruction" not in cmd, f"--instruction must not appear in consumer mode: {cmd_str}"

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_tick_flag_off_unchanged_with_dispatches(self, mock_kill, mock_popen, mock_wt, tmp_path):
        """Flag off + 6 queued dispatches → existing generic instruction still used (no regression)."""
        wt = tmp_path / "worktrees" / "worker"
        wt.mkdir(parents=True)
        mock_wt.return_value = wt
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=6)
        _enqueue(db_path, 6)

        mgr = PoolManager("vnx-dev", "default", db_path)

        env = {k: v for k, v in os.environ.items() if k != "VNX_POOL_TASK_CONSUMER"}
        with patch.dict(os.environ, env, clear=True):
            result = mgr.tick()

        assert len(result.spawned) >= 1
        for call in mock_popen.call_args_list:
            cmd = call[0][0]
            cmd_str = " ".join(cmd)
            assert "subprocess_dispatch" in cmd_str
            assert "--instruction" in cmd
            instr_idx = cmd.index("--instruction") + 1
            assert "Pool worker" in cmd[instr_idx]

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_tick_spawned_terminal_ids_are_distinct(self, mock_kill, mock_popen, mock_wt, tmp_path):
        """Each spawned worker gets a distinct terminal_id (ADR-018 FM-4: no overlap)."""
        wt = tmp_path / "worktrees" / "worker"
        wt.mkdir(parents=True)
        mock_wt.return_value = wt
        pids = iter(range(10000, 10010))
        def make_proc(*a, **kw):
            proc = MagicMock()
            proc.pid = next(pids)
            return proc
        mock_popen.side_effect = make_proc

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=6)
        _enqueue(db_path, 6)

        mgr = PoolManager("vnx-dev", "default", db_path)

        with patch.dict(os.environ, {"VNX_POOL_TASK_CONSUMER": "1"}):
            result = mgr.tick()

        # terminal_ids returned by tick must be distinct
        assert len(result.spawned) == len(set(result.spawned))
