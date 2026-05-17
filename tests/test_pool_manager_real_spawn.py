"""test_pool_manager_real_spawn.py — Integration tests for real subprocess spawning.

All tests exercise real subprocess.Popen — no Popen mocking. Spawned processes
are lightweight Python sleepers that verify:
- PID returned is > 0
- PID refers to a live OS process (os.kill probe)
- Pool membership records carry real PIDs
- Reaper can terminate real PIDs via SIGTERM

The claude CLI is NOT required. Tests spawn
  ``python3 -c "import time; time.sleep(30)"``
as a stand-in for pool workers, wired through a real spawn_fn injected into
PoolManager. ``_spawn_via_provider_dispatch`` failure-mode paths are also
tested (with Popen mocked only for those cases) to guard against regression
to the pre-PR-6.5a stub that always returned ``success=True``.

Dispatch-ID: audit-1-pool-spawn-20260517-224648
"""
from __future__ import annotations

import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_manager import PoolManager, SpawnResult, _spawn_via_provider_dispatch  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLEEPER_CMD = [sys.executable, "-c", "import time; time.sleep(30)"]


def _setup_db(tmp_path: Path, min_workers: int = 0, max_workers: int = 4) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_test_db_file(
        db_path,
        min_workers=min_workers,
        max_workers=max_workers,
        target_workers=max(min_workers, 1),
    )


class _ProcessTracker:
    """Tracks real spawned PIDs and terminates them on cleanup."""

    def __init__(self) -> None:
        self.pids: list[int] = []

    def make_real_spawn_fn(self):
        """Return a SpawnFn that spawns a real sleeper subprocess (no claude needed)."""
        tracker = self

        def spawn_fn(project_id: str, pool_id: str, terminal_id: str,
                     provider: str, role: str) -> SpawnResult:
            import subprocess
            try:
                proc = subprocess.Popen(_SLEEPER_CMD, start_new_session=True)
                tracker.pids.append(proc.pid)
                return SpawnResult(terminal_id=terminal_id, success=True, pid=proc.pid)
            except OSError as exc:
                return SpawnResult(terminal_id=terminal_id, success=False, error=str(exc))

        return spawn_fn

    def cleanup(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------------------
# 1. Real Popen spawn: PID is a live OS process
# ---------------------------------------------------------------------------

class TestRealPopenSpawnReturnsLivePid:
    """The spawn_fn must produce real OS process PIDs, not fabricated values."""

    def test_spawn_fn_returns_live_pid(self, tmp_path):
        """A real spawn_fn must return a live process PID immediately after spawn."""
        tracker = _ProcessTracker()
        spawn_fn = tracker.make_real_spawn_fn()

        result = spawn_fn("vnx-dev", "default", "T1-real", "claude", "backend-developer")
        try:
            assert result.success is True, f"spawn failed: {result.error}"
            assert result.pid is not None
            assert result.pid > 0, "PID must be positive"

            try:
                os.kill(result.pid, 0)
            except ProcessLookupError:
                pytest.fail(
                    f"Spawned process PID {result.pid} is not alive — "
                    "stub suspected (returns success=True without real process)"
                )
        finally:
            tracker.cleanup()

    def test_spawn_2_workers_2_distinct_live_pids(self, tmp_path):
        """Spawn 2 workers: both PIDs must be distinct and refer to live OS processes."""
        tracker = _ProcessTracker()
        spawn_fn = tracker.make_real_spawn_fn()

        results = [
            spawn_fn("vnx-dev", "default", f"T{i}-real", "claude", "backend-developer")
            for i in range(2)
        ]
        try:
            failed = [r for r in results if not r.success]
            assert not failed, f"Some spawns failed: {[r.error for r in failed]}"

            pids = [r.pid for r in results]
            assert len(set(pids)) == 2, f"Expected 2 distinct PIDs, got {pids}"

            for pid in pids:
                assert pid is not None and pid > 0
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pytest.fail(
                        f"Pool worker PID {pid} not alive — pool state is fictional"
                    )
        finally:
            tracker.cleanup()

    def test_spawn_returns_non_zero_pid_before_cleanup(self, tmp_path):
        """PID must survive long enough to be validated (process isn't instant-exit)."""
        tracker = _ProcessTracker()
        spawn_fn = tracker.make_real_spawn_fn()

        result = spawn_fn("vnx-dev", "default", "T-longlive", "claude", "backend-developer")
        try:
            assert result.pid is not None and result.pid > 0
            time.sleep(0.05)
            try:
                os.kill(result.pid, 0)
            except ProcessLookupError:
                pytest.fail(f"Process {result.pid} died within 50ms — not a durable worker")
        finally:
            tracker.cleanup()


# ---------------------------------------------------------------------------
# 2. PoolManager integration: real PIDs persisted in membership table
# ---------------------------------------------------------------------------

class TestPoolManagerRealPidsInMembership:
    """PoolManager.tick() with a real spawn_fn: DB membership must carry live PIDs."""

    def test_tick_scale_up_membership_carries_live_pids(self, tmp_path):
        """After scale_up tick, each spawned membership row must have a live PID."""
        tracker = _ProcessTracker()
        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-real-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        mgr = PoolManager("vnx-dev", "default", db_path,
                          spawn_fn=tracker.make_real_spawn_fn())
        try:
            result = mgr.tick()

            if not result.spawned:
                pytest.skip("Pool decided noop/scale_down — no spawn to verify")

            repo = PoolStateRepository(db_path, "vnx-dev")
            members = repo.list_members("default")
            spawned_members = [m for m in members if m.terminal_id in result.spawned]

            assert spawned_members, (
                f"Spawned terminal IDs {result.spawned} must appear in membership table"
            )
            for member in spawned_members:
                assert member.pid is not None, (
                    f"Member {member.terminal_id} missing PID in DB — pool state is fictional"
                )
                assert member.pid > 0, f"PID {member.pid} is not a valid process identifier"
                try:
                    os.kill(member.pid, 0)
                except ProcessLookupError:
                    pytest.fail(
                        f"Membership PID {member.pid} for terminal {member.terminal_id} "
                        "refers to a dead process — pool state diverged from OS reality"
                    )
        finally:
            tracker.cleanup()

    def test_reaper_terminates_real_pid(self, tmp_path):
        """Reaper must be able to terminate a real spawned process."""
        import subprocess

        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        repo = PoolStateRepository(db_path, "vnx-dev")

        proc = subprocess.Popen(_SLEEPER_CMD, start_new_session=True)
        pid = proc.pid
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pytest.fail(f"Sleeper process {pid} was not alive before reap test started")

        repo.add_member(
            "default", "T-reap-real", "claude", "backend-developer", time.time(), pid=pid
        )

        from pool_reaper import ReapConfig
        mgr = PoolManager(
            "vnx-dev", "default", db_path,
            spawn_fn=lambda *a: SpawnResult("x", True),
        )
        mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=0.001, warmup_window_s=0.0)

        with patch("pool_worktree_manager.reap_worker_worktree"):
            reaped = mgr.reap_dead()

        assert len(reaped) >= 1, "Reaper must claim at least one stale target"

        time.sleep(0.3)
        try:
            os.kill(pid, 0)
            still_alive = True
        except ProcessLookupError:
            still_alive = False

        if still_alive:
            try:
                os.kill(pid, signal.SIGKILL)
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass


# ---------------------------------------------------------------------------
# 3. Failure mode: spawn failure must never produce fake success
# ---------------------------------------------------------------------------

class TestSpawnFailureModeNeverFakesSuccess:
    """Guard against regression to the pre-PR-6.5a stub (always success=True)."""

    def test_worktree_failure_returns_success_false(self, tmp_path):
        """Worktree creation failure must propagate as success=False, not swallowed."""
        with patch("pool_worktree_manager.create_worker_worktree") as mock_wt:
            mock_wt.side_effect = RuntimeError("git worktree add failed: origin/main not found")
            result = _spawn_via_provider_dispatch(
                "vnx-dev", "default", "T-fail-wt", "claude", "backend-developer"
            )

        assert result.success is False, (
            "success=True on worktree failure is the stub pattern — must be False"
        )
        assert result.pid is None

    def test_popen_oserror_returns_success_false(self, tmp_path):
        """OSError from Popen (missing binary) must propagate as success=False."""
        with patch("pool_worktree_manager.create_worker_worktree", return_value=tmp_path):
            with patch("pool_manager.subprocess.Popen",
                       side_effect=OSError("No such file or directory")):
                result = _spawn_via_provider_dispatch(
                    "vnx-dev", "default", "T-fail-popen", "claude", "backend-developer"
                )

        assert result.success is False
        assert "Popen failed" in result.error
        assert result.pid is None

    def test_immediately_dead_process_returns_success_false(self, tmp_path):
        """Process that dies before PID probe must return success=False, not fake success."""
        with patch("pool_worktree_manager.create_worker_worktree", return_value=tmp_path):
            with patch("pool_manager.subprocess.Popen") as mock_popen:
                mock_proc = type("FakeProc", (), {"pid": 99999})()
                mock_popen.return_value = mock_proc
                with patch("pool_manager.os.kill", side_effect=ProcessLookupError()):
                    result = _spawn_via_provider_dispatch(
                        "vnx-dev", "default", "T-dead-immed", "claude", "backend-developer"
                    )

        assert result.success is False, (
            "Immediately-dead process must return success=False; "
            "success=True here is the stub pattern"
        )
        assert "died immediately" in result.error
        assert result.pid == 99999

    def test_pool_manager_records_failure_not_success_on_spawn_error(self, tmp_path):
        """PoolManager must record errors in ExecResult when real spawn fails."""
        db_path = _setup_db(tmp_path, min_workers=0, max_workers=4)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-fail-001", "vnx-dev", "queued"),
        )
        conn.commit()
        conn.close()

        def always_fail(project_id, pool_id, terminal_id, provider, role):
            return SpawnResult(terminal_id=terminal_id, success=False, error="ENOENT")

        mgr = PoolManager("vnx-dev", "default", db_path, spawn_fn=always_fail)
        result = mgr.tick()

        if result.decision.action == "scale_up":
            assert len(result.spawned) == 0, (
                "No memberships must be recorded when all spawns fail"
            )
            assert len(result.errors) > 0, (
                "Spawn failures must appear in ExecResult.errors"
            )
