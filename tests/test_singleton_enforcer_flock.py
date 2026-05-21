"""Regression tests for OI-1518: atomic flock-based bash singleton enforcer.

The bug: scripts/singleton_enforcer.sh previously delegated to
vnx_proc_acquire_lock with mode=stop_existing. That path used
mkdir+rm_rf+retry for stale-lock takeover, with a race window where two
parallel contenders could both rm_rf each other's freshly-claimed lock dir
and both proceed as singletons.

The 2026-05-20 receipt-flood incident saw 10x receipt_processor_v4,
8x dispatcher_supervisor, and 8x receipt_processor_supervisor running in
parallel after a burst of pause/resume cycles raced into the cleanup window.

The fix replaces mkdir-based mutex with flock(1) on a fixed FD. flock(2) is
a kernel-level atomic syscall — there is no read-then-claim race.

This module tests:
  1. Single-process: enforce_singleton acquires and reports correctly.
  2. Parallel 10x: exactly one acquires, nine see "Another instance running".
  3. After holder exits, next acquirer succeeds (lock auto-released by kernel).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SINGLETON_SH = VNX_ROOT / "scripts" / "singleton_enforcer.sh"


@pytest.fixture
def singleton_env(tmp_path: Path) -> dict:
    """Tmp dirs for locks/pids; no VNX_HOME leakage to real state."""
    data = tmp_path / ".vnx-data"
    dirs = {
        "VNX_DATA_DIR": data,
        "VNX_STATE_DIR": data / "state",
        "VNX_LOCKS_DIR": data / "locks",
        "VNX_PIDS_DIR": data / "pids",
        "VNX_LOGS_DIR": data / "logs",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    env = {k: str(v) for k, v in dirs.items()}
    env["PATH"] = os.environ.get("PATH", "")
    return env


def _spawn_acquirer(env: dict, name: str, hold_seconds: float) -> subprocess.CompletedProcess:
    """Spawn one bash that sources singleton_enforcer.sh and tries to acquire.

    Returns the completed process; stdout contains "Lock acquired" for the
    winner or "Another instance" for losers.
    """
    script = f"""#!/bin/bash
set -euo pipefail
source "{SINGLETON_SH}"
enforce_singleton "{name}"
sleep {hold_seconds}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture(autouse=True)
def _require_flock():
    if shutil.which("flock") is None:
        pytest.skip("flock(1) not available; install util-linux")


class TestSingleProcess:
    def test_acquire_and_release(self, singleton_env):
        """Single acquirer succeeds; PID file contains its PID."""
        result = _spawn_acquirer(singleton_env, "single_test", hold_seconds=0.1)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "Lock acquired" in result.stdout, f"stdout: {result.stdout}"

        # Lock file may remain (kernel only releases the lock state, not the
        # inode), but the PID file should be cleaned by the EXIT trap.
        pid_file = Path(singleton_env["VNX_PIDS_DIR"]) / "single_test.pid"
        assert not pid_file.exists(), "PID file not cleaned on exit"

    def test_sequential_acquires_both_succeed(self, singleton_env):
        """After first releases, second acquires cleanly."""
        r1 = _spawn_acquirer(singleton_env, "seq_test", hold_seconds=0.1)
        assert r1.returncode == 0
        assert "Lock acquired" in r1.stdout

        r2 = _spawn_acquirer(singleton_env, "seq_test", hold_seconds=0.1)
        assert r2.returncode == 0
        assert "Lock acquired" in r2.stdout, f"second acquire failed: {r2.stdout}"


class TestParallelRace:
    def test_ten_parallel_acquires_exactly_one_wins(self, singleton_env):
        """The receipt-flood scenario: 10 contenders, expect exactly 1 winner.

        Each contender holds the lock for 2s. They all start within a tight
        window. Without flock atomicity (prior mkdir-race design), multiple
        contenders could all see the lock as free and proceed in parallel.
        """
        name = "parallel_test"
        hold = 2.0
        n = 10

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_spawn_acquirer, singleton_env, name, hold) for _ in range(n)]
            results = [f.result() for f in as_completed(futures)]

        winners = [r for r in results if "Lock acquired" in r.stdout]
        losers = [r for r in results if "Another instance" in r.stdout]

        assert len(winners) == 1, (
            f"Expected exactly 1 winner, got {len(winners)}. "
            f"Winners stdout: {[w.stdout for w in winners]}\n"
            f"Losers stdout: {[l.stdout[:200] for l in losers]}"
        )
        assert len(losers) == n - 1, (
            f"Expected {n-1} losers, got {len(losers)}. "
            f"Unclassified: {[r.stdout for r in results if r not in winners and r not in losers]}"
        )

    def test_loser_exits_cleanly_with_zero(self, singleton_env):
        """Losers exit 0 (clean refusal), not 1 (error)."""
        name = "loser_exit_test"
        hold = 1.5

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(_spawn_acquirer, singleton_env, name, hold) for _ in range(3)]
            results = [f.result() for f in as_completed(futures)]

        for r in results:
            assert r.returncode == 0, (
                f"Singleton contender exited non-zero. "
                f"Should be 0 (clean refusal). stdout={r.stdout!r} stderr={r.stderr!r}"
            )


class TestLockRelease:
    def test_lock_released_on_normal_exit(self, singleton_env):
        """After holder exits normally, next acquirer succeeds immediately."""
        name = "release_test"

        # Hold briefly, release.
        r1 = _spawn_acquirer(singleton_env, name, hold_seconds=0.05)
        assert r1.returncode == 0

        # Immediately acquire again — must succeed without delay.
        start = time.time()
        r2 = _spawn_acquirer(singleton_env, name, hold_seconds=0.05)
        elapsed = time.time() - start

        assert r2.returncode == 0
        assert "Lock acquired" in r2.stdout
        assert elapsed < 2.0, f"Re-acquire took {elapsed:.2f}s — kernel did not release lock cleanly"

    def test_lock_released_on_sigkill(self, singleton_env):
        """When holder is SIGKILLed mid-hold, kernel auto-releases lock.

        Models the daemon-crash recovery path: a previous instance died
        without running its trap handler. The flock is released by the
        kernel on process exit (no userland cleanup needed), so the next
        acquirer must succeed even though no trap ran.
        """
        name = "sigkill_test"
        script = f"""#!/bin/bash
set -euo pipefail
source "{SINGLETON_SH}"
enforce_singleton "{name}"
exec sleep 30
"""
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=singleton_env,
        )

        time.sleep(0.5)
        proc.kill()
        proc.wait(timeout=5)

        r = _spawn_acquirer(singleton_env, name, hold_seconds=0.05)
        assert r.returncode == 0
        assert "Lock acquired" in r.stdout, f"After SIGKILL, next acquire failed: {r.stdout}"
