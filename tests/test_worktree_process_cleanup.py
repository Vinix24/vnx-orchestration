"""tests/test_worktree_process_cleanup.py — OI-873: hook process cleanup on worktree teardown.

Verifies that processes still running inside a dispatch worktree are killed
by process group before the worktree is removed, preventing a zombie hook
from holding the coordination DB write lock indefinitely.

Every test must fail against the current (pre-fix) code.  Run:
    pytest tests/test_worktree_process_cleanup.py -v > out.txt 2>&1; echo $?
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Check if a process is actually running (not zombie/defunct).

    ``os.kill(pid, 0)`` succeeds for zombies too — they still exist in the
    process table until their parent reaps them.  A zombie holds no file
    handles or DB locks, so for lock-contention testing we only care about
    processes in a running/sleeping state (not Z).
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        return bool(state) and not state.startswith("Z")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Fall back to os.kill on platforms without ps.
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _pgid_alive(pgid: int) -> bool:
    """Check if any non-zombie process exists in the group."""
    try:
        result = subprocess.run(
            ["ps", "-g", str(pgid), "-o", "pid=,state="],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split()
            if len(parts) >= 2 and not parts[-1].startswith("Z"):
                return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


# ---------------------------------------------------------------------------
# Test: process group of a child inside a worktree is killed
# ---------------------------------------------------------------------------

def test_kill_worktree_processes_kills_child_process(tmp_path: Path):
    """A process spawned with CWD inside the worktree is killed by process group."""
    from worktree_process_cleanup import kill_worktree_processes

    # Create a small test DB inside the worktree that the child will open.
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    # Spawn a Python child that opens the DB and holds a transaction open,
    # keeping its CWD inside tmp_path.
    hold_script = (
        f"import sqlite3, time, os, signal\n"
        f"conn = sqlite3.connect('{db_path}')\n"
        f"conn.execute('BEGIN IMMEDIATE')\n"
        f"# Write PID+PGID to parent via stdout, then hold the lock.\n"
        f"print(f'{{os.getpid()}} {{os.getpgid(0)}}', flush=True)\n"
        f"# Ignore SIGTERM briefly so we can test that SIGKILL follows.\n"
        f"signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"time.sleep(60)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", hold_script],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # Own process group.
    )

    # Read the child's PID and PGID.
    try:
        out_line = proc.stdout.readline()
        child_pid_str, child_pgid_str = out_line.strip().split()
        child_pid = int(child_pid_str)
        child_pgid = int(child_pgid_str)
    except (ValueError, AttributeError):
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("Failed to read child PID/PGID from stdout")

    # Verify the child is alive and (via its own PG) holding the DB lock.
    assert _pid_alive(child_pid), "Child process should be alive before cleanup"
    assert _pgid_alive(child_pgid), "Child process group should be alive before cleanup"

    # Verify the DB is actually locked by the child.
    lock_conn = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        lock_conn.execute("BEGIN IMMEDIATE")
        lock_conn.rollback()
        proc.kill()
        proc.wait(timeout=5)
        lock_conn.close()
        pytest.fail("DB should be locked by child, but BEGIN IMMEDIATE succeeded")
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc).lower(), f"Expected 'locked' error, got: {exc}"
    finally:
        lock_conn.close()

    # Now kill processes in the worktree.
    killed = kill_worktree_processes(tmp_path)
    assert killed > 0, (
        f"No process groups killed. Pre-fix code does not clean up processes "
        f"before worktree removal (OI-873)."
    )

    # Wait for SIGKILL to take effect with a bounded retry loop.
    # SIGKILL cannot be caught/ignored, but the kernel may need a moment
    # to reap the process.
    deadline = time.monotonic() + 5.0
    child_dead = False
    while time.monotonic() < deadline:
        if not _pid_alive(child_pid):
            child_dead = True
            break
        time.sleep(0.1)

    # Child should be dead.
    assert child_dead, (
        f"Child pid {child_pid} should be dead after cleanup. "
        f"Pre-fix code leaves zombie processes holding the DB lock."
    )
    assert not _pgid_alive(child_pgid), (
        f"Child PGID {child_pgid} should be dead after cleanup."
    )

    # Clean up the subprocess if still alive (defensive).
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Test: DB lock is available after process cleanup
# ---------------------------------------------------------------------------

def test_db_lock_available_after_cleanup(tmp_path: Path):
    """After killing worktree processes, BEGIN IMMEDIATE succeeds on the DB."""
    from worktree_process_cleanup import kill_worktree_processes

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    # Spawn a child that holds the DB write lock.
    hold_script = (
        f"import sqlite3, time, signal\n"
        f"conn = sqlite3.connect('{db_path}')\n"
        f"conn.execute('BEGIN IMMEDIATE')\n"
        f"signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"time.sleep(60)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", hold_script],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Give it time to acquire the lock.
    time.sleep(0.3)

    # Verify lock is held.
    lock_conn = sqlite3.connect(str(db_path), timeout=0.5)
    locked = False
    try:
        lock_conn.execute("BEGIN IMMEDIATE")
        lock_conn.rollback()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            locked = True
    lock_conn.close()
    assert locked, "DB should be locked before cleanup"

    # Kill processes.
    kill_worktree_processes(tmp_path)
    time.sleep(0.5)

    # Clean up subprocess.
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Now BEGIN IMMEDIATE should succeed.
    lock_conn2 = sqlite3.connect(str(db_path), timeout=2)
    try:
        lock_conn2.execute("BEGIN IMMEDIATE")
        lock_conn2.execute("INSERT INTO t VALUES (1)")
        lock_conn2.commit()
    except sqlite3.OperationalError as exc:
        lock_conn2.close()
        pytest.fail(
            f"BEGIN IMMEDIATE should succeed after process cleanup, got: {exc}. "
            f"Pre-fix code does not kill zombie processes holding the DB lock (OI-873)."
        )
    lock_conn2.close()


# ---------------------------------------------------------------------------
# Test: kill_worktree_processes does not kill own process group
# ---------------------------------------------------------------------------

def test_kill_does_not_kill_caller(tmp_path: Path):
    """Calling kill_worktree_processes does not kill the caller's own process group."""
    from worktree_process_cleanup import kill_worktree_processes

    # Create a file inside tmp_path so lsof +D finds something.
    (tmp_path / "marker.txt").write_text("test")

    own_pgid = os.getpgid(0)
    killed = kill_worktree_processes(tmp_path)

    # We should still be alive!
    assert os.getpgid(0) == own_pgid, "Our own PGID should be unchanged"

    # The marker file is opened by no process (we already closed it),
    # so lsof +D should find nothing and killed should be 0.
    assert killed == 0, (
        f"Expected 0 killed groups (no processes in worktree), got {killed}"
    )


# ---------------------------------------------------------------------------
# Test: kill_worktree_processes on non-existent directory returns 0
# ---------------------------------------------------------------------------

def test_kill_nonexistent_dir_returns_zero():
    """Killing processes on a non-existent path returns 0 without error."""
    from worktree_process_cleanup import kill_worktree_processes

    result = kill_worktree_processes(Path("/tmp/vnx-nonexistent-worktree-oi873-test"))
    assert result == 0
