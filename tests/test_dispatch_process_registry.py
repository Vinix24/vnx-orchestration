"""tests/test_dispatch_process_registry.py — OI-877: reap dispatch processes outside the worktree.

The worktree-scoped cleanup (``kill_worktree_processes``) finds processes that
have files or CWD inside the dispatch worktree.  OI-877 is the second failure
path: a dispatch process whose repo-root resolved to the MAIN checkout escapes
the worktree entirely, so ``lsof +D <worktree>`` cannot see it.

This suite verifies the pgid-registry teardown: process groups recorded at
dispatch spawn time are re-found at teardown and killed even when they run
outside the worktree and have been reparented to PID 1.

Every test MUST fail against origin/main (the module does not exist there) and
pass on this branch.  Run:
    python -m pytest tests/test_dispatch_process_registry.py > /tmp/out.txt 2>&1; echo "exit=$?"
"""

from __future__ import annotations

import os
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
    """Check if a process is actually running (not zombie/defunct)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        return bool(state) and not state.startswith("Z")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _reap(proc: subprocess.Popen) -> None:
    """Reap a subprocess if it is still around (defensive)."""
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _spawn_sleeping_child(cwd: Path, hold_db: "Path | None" = None) -> "tuple[subprocess.Popen, int, int]":
    """Spawn a child in its own session; return (proc, pid, pgid).

    When *hold_db* is given the child opens it with an immediate transaction,
    holding the write lock, and prints ``pid pgid`` on stdout.
    """
    if hold_db is not None:
        body = (
            f"import sqlite3, time, os\n"
            f"conn = sqlite3.connect('{hold_db}')\n"
            f"conn.execute('BEGIN IMMEDIATE')\n"
            f"print(f'{{os.getpid()}} {{os.getpgid(0)}}', flush=True)\n"
            f"time.sleep(60)\n"
        )
    else:
        body = (
            "import time, os\n"
            "print(f'{os.getpid()} {os.getpgid(0)}', flush=True)\n"
            "time.sleep(60)\n"
        )
    proc = subprocess.Popen(
        [sys.executable, "-c", body],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        out_line = proc.stdout.readline().strip()
        pid_str, pgid_str = out_line.split()
        return proc, int(pid_str), int(pgid_str)
    except (ValueError, AttributeError):
        _reap(proc)
        pytest.fail("Failed to read child PID/PGID from stdout")


# ---------------------------------------------------------------------------
# Core DoD: dispatch child running OUTSIDE the worktree is reaped at teardown
# ---------------------------------------------------------------------------

def test_outside_worktree_dispatch_child_killed(tmp_path: Path):
    """A dispatch child whose CWD is the main checkout (not the worktree) is killed."""
    from dispatch_process_registry import cleanup_dispatch_processes, record_dispatch_pgids

    worktree = tmp_path / "worktree"
    main = tmp_path / "main"
    worktree.mkdir()
    main.mkdir()

    # DB lives in the MAIN checkout, outside the worktree.
    db_path = main / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    # The dispatch child runs from the main checkout and holds the lock —
    # the exact measured shape of OI-877 (cwd outside the worktree).
    proc, child_pid, child_pgid = _spawn_sleeping_child(main, hold_db=db_path)
    assert _pid_alive(child_pid), "child should be alive before teardown"

    # Simulate the spawn-time record (what the dispatch lane records).
    record_dispatch_pgids("dispatch-oi877", [child_pgid], repo_root=tmp_path)

    # Worktree scan alone cannot see the child (it is outside the worktree).
    from worktree_process_cleanup import kill_worktree_processes
    assert kill_worktree_processes(worktree) == 0, (
        "precondition: the child must be invisible to the worktree scan"
    )

    killed = cleanup_dispatch_processes("dispatch-oi877", worktree, repo_root=tmp_path)
    assert killed >= 1, (
        "cleanup must find the dispatch child via the recorded PGID "
        "(OI-877 core DoD)"
    )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(child_pid):
        time.sleep(0.1)
    assert not _pid_alive(child_pid), (
        f"child pid {child_pid} should be dead after pgid-based cleanup"
    )
    _reap(proc)


# ---------------------------------------------------------------------------
# Reparented (PPID 1) child is still recognised
# ---------------------------------------------------------------------------

def test_reparented_child_still_killed(tmp_path: Path):
    """A dispatch child reparented to PID 1 keeps its PGID and is still reaped."""
    from dispatch_process_registry import cleanup_dispatch_processes, record_dispatch_pgids

    worktree = tmp_path / "worktree"
    main = tmp_path / "main"
    worktree.mkdir()
    main.mkdir()

    # Launcher spawns a grandchild in its own session, prints its pid/pgid,
    # then exits — the grandchild is reparented to launchd (PPID 1).
    launcher_file = tmp_path / "launcher.py"
    launcher_file.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', "
        "'import os,sys,time; print(os.getpid(), os.getpgid(0), flush=True); time.sleep(60)'],\n"
        "    start_new_session=True, stdout=subprocess.PIPE, text=True,\n"
        ")\n"
        "print(child.stdout.readline().strip(), flush=True)\n",
        encoding="utf-8",
    )
    launcher = subprocess.Popen(
        [sys.executable, str(launcher_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    out_line = launcher.stdout.readline().strip()
    grandchild_pid_str, grandchild_pgid_str = out_line.split()
    grandchild_pid = int(grandchild_pid_str)
    grandchild_pgid = int(grandchild_pgid_str)

    launcher.wait(timeout=10)
    assert launcher.returncode == 0, "launcher should exit cleanly"

    # Verify the grandchild is now reparented to PID 1.
    ps = subprocess.run(
        ["ps", "-p", str(grandchild_pid), "-o", "ppid="],
        capture_output=True, text=True, timeout=5,
    )
    assert ps.stdout.strip() == "1", (
        f"expected grandchild reparented to PPID 1, got {ps.stdout.strip()}"
    )
    assert _pid_alive(grandchild_pid), "grandchild should still be alive"

    record_dispatch_pgids("dispatch-reparent", [grandchild_pgid], repo_root=tmp_path)

    killed = cleanup_dispatch_processes("dispatch-reparent", worktree, repo_root=tmp_path)
    assert killed >= 1, "reparented child must be reaped via its recorded PGID"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(grandchild_pid):
        time.sleep(0.1)
    assert not _pid_alive(grandchild_pid), (
        f"reparented child {grandchild_pid} should be dead after cleanup"
    )


# ---------------------------------------------------------------------------
# The dispatcher's own process group is never touched
# ---------------------------------------------------------------------------

def test_own_process_group_untouched(tmp_path: Path):
    """Recording + cleaning a dispatch whose PGID is the caller's own is a no-op."""
    from dispatch_process_registry import kill_dispatch_pgids, record_dispatch_pgids

    own_pgid = os.getpgid(0)
    record_dispatch_pgids("dispatch-self", [own_pgid], repo_root=tmp_path)

    killed = kill_dispatch_pgids("dispatch-self", repo_root=tmp_path)
    assert killed == 0, "own process group must never be signalled"
    assert os.getpgid(0) == own_pgid, "caller's PGID must be unchanged"


# ---------------------------------------------------------------------------
# A process of ANOTHER dispatch is never touched
# ---------------------------------------------------------------------------

def test_other_dispatch_untouched(tmp_path: Path):
    """Cleaning dispatch-B must not touch a child recorded for dispatch-A."""
    from dispatch_process_registry import cleanup_dispatch_processes, record_dispatch_pgids

    worktree = tmp_path / "worktree"
    main = tmp_path / "main"
    worktree.mkdir()
    main.mkdir()

    proc, child_pid, child_pgid = _spawn_sleeping_child(main)
    record_dispatch_pgids("dispatch-A", [child_pgid], repo_root=tmp_path)

    killed = cleanup_dispatch_processes("dispatch-B", worktree, repo_root=tmp_path)
    assert killed == 0, "cleaning a different dispatch must kill nothing"
    assert _pid_alive(child_pid), (
        f"child {child_pid} of dispatch-A must survive cleaning dispatch-B"
    )

    # Cleaning dispatch-A itself does kill it.
    killed_a = cleanup_dispatch_processes("dispatch-A", worktree, repo_root=tmp_path)
    assert killed_a >= 1
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(child_pid):
        time.sleep(0.1)
    assert not _pid_alive(child_pid)
    _reap(proc)


# ---------------------------------------------------------------------------
# The existing worktree-scoped cleanup still works (through the combined path)
# ---------------------------------------------------------------------------

def test_worktree_based_cleanup_still_works(tmp_path: Path):
    """A child inside the worktree is still reaped by the lsof-based scan."""
    from dispatch_process_registry import cleanup_dispatch_processes

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    db_path = worktree / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    proc, child_pid, _child_pgid = _spawn_sleeping_child(worktree, hold_db=db_path)
    assert _pid_alive(child_pid)

    # No pgid recorded: the combined cleanup must still catch the child via the
    # worktree scan.
    killed = cleanup_dispatch_processes("dispatch-wt", worktree, repo_root=tmp_path)
    assert killed >= 1, "worktree-based cleanup must still work through the combined path"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(child_pid):
        time.sleep(0.1)
    assert not _pid_alive(child_pid)
    _reap(proc)


# ---------------------------------------------------------------------------
# Registry hygiene
# ---------------------------------------------------------------------------

def test_record_load_clear_roundtrip(tmp_path: Path):
    """Recording, loading and clearing the registry round-trips correctly."""
    from dispatch_process_registry import (
        clear_dispatch_pgids,
        load_dispatch_pgids,
        record_dispatch_pgids,
    )

    # Use the caller's own group so the record has live members to snapshot.
    own_pgid = os.getpgid(0)
    record_dispatch_pgids("dispatch-roundtrip", [own_pgid], repo_root=tmp_path)
    assert load_dispatch_pgids("dispatch-roundtrip", repo_root=tmp_path) == [own_pgid]

    clear_dispatch_pgids("dispatch-roundtrip", repo_root=tmp_path)
    assert load_dispatch_pgids("dispatch-roundtrip", repo_root=tmp_path) == []


def test_no_entry_kills_nothing(tmp_path: Path):
    """Cleaning a dispatch with no recorded pgids is a no-op (returns 0)."""
    from dispatch_process_registry import cleanup_dispatch_processes

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    killed = cleanup_dispatch_processes("dispatch-absent", worktree, repo_root=tmp_path)
    assert killed == 0
