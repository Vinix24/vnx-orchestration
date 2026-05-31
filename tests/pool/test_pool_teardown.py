"""test_pool_teardown.py — OI-010 pool teardown leak fix.

Verifies that scale_down and _execute_reap both:
1. Signal (terminate) the stored PID via _kill_subprocess
2. Remove the git worktree via reap_worker_worktree

Tests use a real temp git repo with an actual worktree so that
`git worktree list` assertions are meaningful.

Dispatch-ID: 20260529-160653-pool-teardown
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_decision_engine import PoolDecision
from pool_manager import PoolManager, SpawnResult
from pool_state_repo import PoolStateRepository
from pool_state_fixtures import create_test_db_file, insert_lease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_db(tmp_path: Path, min_workers: int = 0, max_workers: int = 4) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_test_db_file(
        db_path,
        min_workers=min_workers,
        max_workers=max_workers,
        target_workers=max(min_workers, 1),
    )


def _make_git_repo(base: Path) -> Path:
    """Init a minimal git repo with one commit; return repo root."""
    base.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(base)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(base), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(base), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (base / "init.txt").write_text("init")
    subprocess.run(["git", "-C", str(base), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(base), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return base


def _create_worktree(git_root: Path, terminal_id: str) -> Path:
    """Create a real pool worktree for terminal_id under git_root."""
    wt_path = git_root / ".vnx-data" / "worktrees" / f"pool-{terminal_id}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch_name = f"pool/{terminal_id}"
    subprocess.run(
        ["git", "-C", str(git_root), "worktree", "add", "-b", branch_name, str(wt_path)],
        check=True, capture_output=True,
    )
    return wt_path


def _worktree_names(git_root: Path) -> list:
    """Return list of worktree paths registered with git worktree list."""
    result = subprocess.run(
        ["git", "-C", str(git_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    return [
        line.split()[1]
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _add_member_with_pid(
    db_path: Path,
    terminal_id: str,
    pid: int,
    project_id: str = "vnx-dev",
) -> str:
    """Insert lease + membership with pid; returns membership_id."""
    conn = sqlite3.connect(str(db_path))
    insert_lease(conn, terminal_id, project_id)
    conn.close()
    repo = PoolStateRepository(db_path, project_id)
    membership_id = repo.add_member(
        "default", terminal_id, "claude", "backend-developer", 1000.0, pid=pid
    )
    return membership_id


# ---------------------------------------------------------------------------
# scale_down terminates PID + removes worktree
# ---------------------------------------------------------------------------

class TestScaleDownTeardown:
    def test_scale_down_kills_pid_and_removes_worktree(self, tmp_path):
        git_root = _make_git_repo(tmp_path / "repo")
        terminal_id = "TERM-SD1"
        fake_pid = 19991

        wt_path = _create_worktree(git_root, terminal_id)
        assert wt_path.is_dir(), "worktree must exist before scale_down"

        db_path = _setup_db(tmp_path)
        membership_id = _add_member_with_pid(db_path, terminal_id, fake_pid)

        killed_pids = []

        def fake_kill(tid, pid):
            killed_pids.append((tid, pid))

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr._kill_subprocess = fake_kill

        decision = PoolDecision(
            action="scale_down",
            delta=-1,
            reason="test",
            targets=[membership_id],
        )

        with patch(
            "pool_worktree_manager._resolve_project_root",
            return_value=git_root,
        ):
            result = mgr.execute(decision)

        assert membership_id in result.reaped
        assert not result.errors

        # PID was signalled
        assert (terminal_id, fake_pid) in killed_pids, (
            f"expected kill({terminal_id}, {fake_pid}), got {killed_pids}"
        )

        # Worktree directory gone from disk
        assert not wt_path.exists(), "worktree directory should be removed after scale_down"

        # git worktree list shows no pool-TERM-SD1
        names = _worktree_names(git_root)
        assert not any("pool-TERM-SD1" in n for n in names), (
            f"git worktree list still shows pool-TERM-SD1: {names}"
        )

    def test_scale_down_safe_when_pid_already_dead(self, tmp_path):
        """_kill_subprocess raising does not abort the worktree cleanup."""
        git_root = _make_git_repo(tmp_path / "repo")
        terminal_id = "TERM-SD2"
        fake_pid = 29992

        wt_path = _create_worktree(git_root, terminal_id)

        db_path = _setup_db(tmp_path)
        membership_id = _add_member_with_pid(db_path, terminal_id, fake_pid)

        def kill_raises(tid, pid):
            raise ProcessLookupError(f"no such process: {pid}")

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr._kill_subprocess = kill_raises

        decision = PoolDecision(
            action="scale_down", delta=-1, reason="test", targets=[membership_id]
        )

        with patch(
            "pool_worktree_manager._resolve_project_root",
            return_value=git_root,
        ):
            result = mgr.execute(decision)

        # Kill failure must not propagate as an ExecResult error
        assert membership_id in result.reaped
        assert not result.errors

        # Worktree still gets cleaned up despite kill failure
        assert not wt_path.exists()

    def test_scale_down_safe_when_worktree_already_gone(self, tmp_path):
        """reap_worker_worktree on absent worktree must not error."""
        git_root = _make_git_repo(tmp_path / "repo")
        terminal_id = "TERM-SD3"
        fake_pid = 39993

        # Intentionally do NOT create the worktree — it's already gone
        db_path = _setup_db(tmp_path)
        membership_id = _add_member_with_pid(db_path, terminal_id, fake_pid)

        killed_pids = []
        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr._kill_subprocess = lambda tid, pid: killed_pids.append((tid, pid))

        decision = PoolDecision(
            action="scale_down", delta=-1, reason="test", targets=[membership_id]
        )

        with patch(
            "pool_worktree_manager._resolve_project_root",
            return_value=git_root,
        ):
            result = mgr.execute(decision)

        assert membership_id in result.reaped
        assert not result.errors
        assert (terminal_id, fake_pid) in killed_pids


# ---------------------------------------------------------------------------
# _execute_reap terminates PID + removes worktree
# ---------------------------------------------------------------------------

class TestExecuteReapTeardown:
    def test_execute_reap_kills_pid_and_removes_worktree(self, tmp_path):
        git_root = _make_git_repo(tmp_path / "repo")
        terminal_id = "TERM-RP1"
        fake_pid = 49994

        wt_path = _create_worktree(git_root, terminal_id)
        assert wt_path.is_dir()

        db_path = _setup_db(tmp_path)
        membership_id = _add_member_with_pid(db_path, terminal_id, fake_pid)

        killed_pids = []
        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr._kill_subprocess = lambda tid, pid: killed_pids.append((tid, pid))

        decision = PoolDecision(
            action="reap",
            delta=0,
            reason="heartbeat_stale",
            targets=[membership_id],
        )

        with patch(
            "pool_worktree_manager._resolve_project_root",
            return_value=git_root,
        ):
            result = mgr.execute(decision)

        assert membership_id in result.reaped
        assert not result.errors

        assert (terminal_id, fake_pid) in killed_pids, (
            f"expected kill({terminal_id}, {fake_pid}), got {killed_pids}"
        )

        assert not wt_path.exists(), "worktree directory should be removed after reap"

        names = _worktree_names(git_root)
        assert not any("pool-TERM-RP1" in n for n in names), (
            f"git worktree list still shows pool-TERM-RP1: {names}"
        )

    def test_execute_reap_handles_missing_member_gracefully(self, tmp_path):
        """target membership_id with no matching member: no crash, no leak."""
        db_path = _setup_db(tmp_path)

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=lambda *a: SpawnResult("x", True))
        mgr._kill_subprocess = MagicMock()

        decision = PoolDecision(
            action="reap",
            delta=0,
            reason="test",
            targets=["nonexistent-membership-id"],
        )

        with patch("pool_worktree_manager.reap_worker_worktree") as mock_reap:
            result = mgr.execute(decision)

        # Reap of nonexistent membership raises → error recorded, no crash
        # kill and worktree cleanup should NOT be called for unknown member
        mgr._kill_subprocess.assert_not_called()
        mock_reap.assert_not_called()
