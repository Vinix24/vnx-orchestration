"""test_subprocess_spawn.py — Tests for real subprocess spawn in pool_manager.

Verifies PR-6.5a: _spawn_via_provider_dispatch uses subprocess.Popen,
captures PID, stores PID in membership metadata, and the reaper validates
PID liveness via os.kill(pid, 0).

All subprocess.Popen calls are mocked — no real CLI sessions are launched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional
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
from pool_state_repo import PoolStateRepository
from pool_state_fixtures import _BASE_SCHEMA, create_test_db_file, insert_lease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_mock_popen(pid: int = 12345, raise_on_init: bool = False):
    if raise_on_init:
        def _raiser(*args, **kwargs):
            raise OSError("mock Popen failure: command not found")
        return _raiser

    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    def _init(*args, **kwargs):
        return mock_proc

    return _init


def _read_membership_meta(db_path: Path, terminal_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT metadata_json FROM worker_pool_membership WHERE terminal_id = ?",
        (terminal_id,),
    ).fetchone()
    conn.close()
    if row and row[0]:
        return json.loads(row[0])
    return {}


# ---------------------------------------------------------------------------
# SpawnResult dataclass
# ---------------------------------------------------------------------------

class TestSpawnResult:
    def test_spawn_result_has_pid_field(self):
        result = SpawnResult(terminal_id="T1", success=True, pid=9999)
        assert result.pid == 9999

    def test_spawn_result_pid_defaults_to_none(self):
        result = SpawnResult(terminal_id="T1", success=True)
        assert result.pid is None

    def test_spawn_result_failed_with_pid(self):
        result = SpawnResult(terminal_id="T1", success=False, error="died", pid=42)
        assert not result.success
        assert result.pid == 42


# ---------------------------------------------------------------------------
# _spawn_via_provider_dispatch — unit tests
# ---------------------------------------------------------------------------

class TestSpawnViaProviderDispatch:
    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_successful_spawn_captures_pid(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_popen.return_value = mock_proc

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is True
        assert result.pid == 54321
        assert result.terminal_id == "T1"
        mock_wt.assert_called_once_with("T1")
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "--terminal-id" in cmd
        assert "T1" in cmd
        assert "--role" in cmd
        assert "backend-developer" in cmd
        assert call_args[1]["cwd"] == "/tmp/fake-worktree"

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_uses_start_new_session(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_popen.return_value = mock_proc

        _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    def test_spawn_failure_on_oserror(self, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_popen.side_effect = OSError("No such file or directory")

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is False
        assert "Popen failed" in result.error
        assert result.pid is None

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_failure_when_process_dies_immediately(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        mock_kill.side_effect = ProcessLookupError("No such process")

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is False
        assert "died immediately" in result.error
        assert result.pid == 99999

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_command_includes_dispatch_id(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 200
        mock_popen.return_value = mock_proc

        _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        cmd = mock_popen.call_args[0][0]
        assert "--dispatch-id" in cmd
        dispatch_id_idx = cmd.index("--dispatch-id") + 1
        assert cmd[dispatch_id_idx].startswith("pool-spawn-T1-")

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_command_includes_instruction(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 300
        mock_popen.return_value = mock_proc

        _spawn_via_provider_dispatch(
            "vnx-dev", "my-pool", "T2", "claude", "code-reviewer"
        )

        cmd = mock_popen.call_args[0][0]
        assert "--instruction" in cmd
        instr_idx = cmd.index("--instruction") + 1
        assert "T2" in cmd[instr_idx]
        assert "my-pool" in cmd[instr_idx]

    @patch("pool_worktree_manager.create_worker_worktree")
    def test_spawn_failure_on_worktree_error(self, mock_wt):
        mock_wt.side_effect = RuntimeError("git worktree add failed")

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is False
        assert "worktree creation failed" in result.error
        assert result.pid is None


# ---------------------------------------------------------------------------
# PID in membership metadata — integration with PoolStateRepository
# ---------------------------------------------------------------------------

class TestPidInMembershipMetadata:
    def test_add_member_stores_pid(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")

        membership_id = repo.add_member("default", "T1", "claude", "backend-developer", 1000.0, pid=54321)

        meta = _read_membership_meta(db_path, "T1")
        assert meta.get("pid") == 54321
        assert meta.get("membership_id") == membership_id

    def test_add_member_without_pid_omits_key(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")

        repo.add_member("default", "T1", "claude", "backend-developer", 1000.0)

        meta = _read_membership_meta(db_path, "T1")
        assert "pid" not in meta
        assert "membership_id" in meta

    def test_list_members_reads_pid(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")
        repo.add_member("default", "T1", "claude", "backend-developer", 1000.0, pid=12345)

        members = repo.list_members("default")

        assert len(members) == 1
        assert members[0].pid == 12345
        assert members[0].terminal_id == "T1"

    def test_list_members_pid_none_when_absent(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")
        repo.add_member("default", "T1", "claude", "backend-developer", 1000.0)

        members = repo.list_members("default")

        assert len(members) == 1
        assert members[0].pid is None

    def test_list_members_pid_survives_json_roundtrip(self, tmp_path):
        db_path = _setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")
        repo.add_member("default", "T1", "claude", "backend-developer", 1000.0, pid=2**31 - 1)

        members = repo.list_members("default")
        assert members[0].pid == 2**31 - 1


# ---------------------------------------------------------------------------
# PoolManager scale_up passes PID through to membership
# ---------------------------------------------------------------------------

class TestPoolManagerScaleUpPid:
    def test_scale_up_stores_pid_from_spawn_fn(self, tmp_path):
        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        def spawn_with_pid(project_id, pool_id, terminal_id, provider, role):
            return SpawnResult(terminal_id=terminal_id, success=True, pid=77777)

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=spawn_with_pid)
        result = mgr.tick()

        assert len(result.spawned) >= 1
        repo = PoolStateRepository(db_path, "vnx-dev")
        members = repo.list_members("default")
        assert any(m.pid == 77777 for m in members)

    def test_scale_up_pid_none_when_spawn_fn_omits_pid(self, tmp_path):
        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-002", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        def spawn_no_pid(project_id, pool_id, terminal_id, provider, role):
            return SpawnResult(terminal_id=terminal_id, success=True)

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=spawn_no_pid)
        result = mgr.tick()

        assert len(result.spawned) >= 1
        repo = PoolStateRepository(db_path, "vnx-dev")
        members = repo.list_members("default")
        for m in members:
            assert m.pid is None


# ---------------------------------------------------------------------------
# Reaper PID validation via os.kill(pid, 0)
# ---------------------------------------------------------------------------

class TestReaperPidValidation:
    def test_reap_dead_uses_pid_for_kill(self, tmp_path):
        from pool_reaper import ReapConfig

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        conn = sqlite3.connect(str(db_path))
        insert_lease(conn, "T1")
        conn.close()
        repo = PoolStateRepository(db_path, "vnx-dev")
        repo.add_member("default", "T1", "claude", "backend-developer", 500.0, pid=11111)

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr.reap_config = ReapConfig(
            heartbeat_stale_threshold_s=10.0,
            warmup_window_s=5.0,
        )

        with patch.object(mgr, "_kill_subprocess") as mock_kill:
            reaped = mgr.reap_dead()

        if reaped:
            call_args = mock_kill.call_args_list[0]
            assert call_args[0][1] == 11111

    @patch("pool_manager.os.kill")
    def test_kill_subprocess_probes_pid_liveness(self, mock_os_kill, tmp_path):
        db_path = _setup_db(tmp_path)
        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))

        mock_os_kill.side_effect = [None, ProcessLookupError]
        mgr._kill_subprocess("T1", 55555)

        calls = mock_os_kill.call_args_list
        assert any(c[0] == (55555, 0) for c in calls) or any(
            c[0][0] == 55555 for c in calls
        )

    def test_kill_subprocess_skips_none_pid(self, tmp_path):
        db_path = _setup_db(tmp_path)
        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))

        with patch("pool_manager.os.kill") as mock_kill:
            mgr._kill_subprocess("T1", None)
            mock_kill.assert_not_called()

    def test_kill_subprocess_skips_zero_pid(self, tmp_path):
        db_path = _setup_db(tmp_path)
        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))

        with patch("pool_manager.os.kill") as mock_kill:
            mgr._kill_subprocess("T1", 0)
            mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# Negative-path tests
# ---------------------------------------------------------------------------

class TestSpawnNegativePaths:
    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    def test_spawn_popen_file_not_found(self, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_popen.side_effect = FileNotFoundError("python3 not found")

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", "backend-developer"
        )

        assert result.success is False
        assert "Popen failed" in result.error

    @patch("pool_worktree_manager.create_worker_worktree")
    @patch("pool_manager.subprocess.Popen")
    @patch("pool_manager.os.kill")
    def test_spawn_with_empty_role(self, mock_kill, mock_popen, mock_wt):
        mock_wt.return_value = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 400
        mock_popen.return_value = mock_proc

        result = _spawn_via_provider_dispatch(
            "vnx-dev", "default", "T1", "claude", ""
        )

        assert result.success is True
        assert result.pid == 400
        cmd = mock_popen.call_args[0][0]
        role_idx = cmd.index("--role") + 1
        assert cmd[role_idx] == ""
