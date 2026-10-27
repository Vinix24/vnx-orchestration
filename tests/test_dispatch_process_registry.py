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

import json
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


# ---------------------------------------------------------------------------
# Tri-state liveness (PR-4a, round-3 finding)
# ---------------------------------------------------------------------------

def test_group_liveness_is_three_valued(monkeypatch):
    """A probe error other than "no such process" is ``unknown``, never dead.

    Red run on the old ``_group_is_valid``: its ``PermissionError`` branch
    returns False, so the unknown case is reported as dead.

    Red run on 70b23b41: with every recorded member gone the old probe answered
    ``dead`` without asking the group itself, so a child that outlived its
    parent was classified dead.  Absence from the recorded snapshot is not
    group absence: only ``killpg(pgid, 0)`` answering ``ProcessLookupError``
    proves the group is gone.
    """
    from dispatch_process_registry import (
        GROUP_ALIVE,
        GROUP_DEAD,
        GROUP_UNKNOWN,
        probe_group_liveness,
    )

    monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
    assert probe_group_liveness(4242, [1, 2]).state == GROUP_ALIVE

    def _gone(pid):
        raise ProcessLookupError(pid)

    def _denied(pid):
        raise PermissionError(pid)

    def _killpg_alive(pgid, sig):
        return None  # the group still exists (or the PGID was reused)

    def _killpg_denied(pgid, sig):
        raise PermissionError(pgid)

    def _killpg_missing(pgid, sig):
        raise ProcessLookupError(pgid)

    def _killpg_unmeasurable(pgid, sig):
        raise OSError("probe unavailable")

    monkeypatch.setattr(os, "getpgid", _gone)

    # No recorded member carries the PGID any more, but the group itself is
    # still there: a child spawned after the snapshot, or a reused PGID.  Both
    # are "alive" for a removal guard — the safe side.
    monkeypatch.setattr(os, "killpg", _killpg_alive)
    assert probe_group_liveness(4242, [1, 2]).state == GROUP_ALIVE

    # A signal probe the kernel refuses for lack of permission still proves
    # the group exists: that is alive, not unknown.
    monkeypatch.setattr(os, "killpg", _killpg_denied)
    assert probe_group_liveness(4242, [1, 2]).state == GROUP_ALIVE

    # Only ProcessLookupError from the group probe is evidence of absence.
    monkeypatch.setattr(os, "killpg", _killpg_missing)
    assert probe_group_liveness(4242, [1, 2]).state == GROUP_DEAD

    # Any other OSError means the group could not be measured: unknown.
    monkeypatch.setattr(os, "killpg", _killpg_unmeasurable)
    probe = probe_group_liveness(4242, [1, 2])
    assert probe.state == GROUP_UNKNOWN
    assert probe.detail, "an unknown group must name why it could not be measured"

    monkeypatch.setattr(os, "getpgid", _denied)
    probe = probe_group_liveness(4242, [1, 2])
    assert probe.state == GROUP_UNKNOWN
    assert probe.detail, "an unknown group must name why it could not be measured"

    # No recorded members is not positive evidence either.
    monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
    assert probe_group_liveness(4242, []).state == GROUP_UNKNOWN


def test_live_process_groups_reports_unknown_separately(tmp_path, monkeypatch):
    """``live_process_groups`` keeps an unmeasurable group out of ``dead`` and
    makes the report truthy: unknown answers "yes, keep"."""
    from dispatch_process_registry import live_process_groups, record_dispatch_pgids

    assert not live_process_groups("dispatch-no-entry", repo_root=tmp_path)

    own_pgid = os.getpgid(0)
    record_dispatch_pgids("dispatch-unknown", [own_pgid], repo_root=tmp_path)

    def _denied(pid):
        raise PermissionError(pid)

    monkeypatch.setattr(os, "getpgid", _denied)
    report = live_process_groups("dispatch-unknown", repo_root=tmp_path)
    assert report.alive == ()
    assert report.dead == ()
    assert own_pgid in report.unknown
    assert bool(report) is True, "an unknown group must read as 'keep', never as nothing alive"


def test_live_process_groups_unreadable_entry_is_unmeasurable(tmp_path):
    from dispatch_process_registry import live_process_groups

    corrupt = tmp_path / ".vnx-data" / "state" / "dispatch_pgids" / "dispatch-corrupt.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not json", encoding="utf-8")

    report = live_process_groups("dispatch-corrupt", repo_root=tmp_path)
    assert report.unmeasurable, "an unreadable entry is not evidence that nothing is alive"
    assert bool(report) is True


# ---------------------------------------------------------------------------
# A reused PGID must never become a kill target (PR-4a round 2)
# ---------------------------------------------------------------------------

def _write_registry(tmp_path: Path, dispatch_id: str, groups: "dict[int, list[int]]") -> Path:
    """Write a registry entry directly, with exactly the recorded members given."""
    path = tmp_path / ".vnx-data" / "state" / "dispatch_pgids" / f"{dispatch_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "groups": {str(pgid): members for pgid, members in groups.items()},
            }
        ),
        encoding="utf-8",
    )
    return path


def _reaped_pid() -> int:
    """Return the pid of a process that has exited and been waited for."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def test_reused_pgid_alone_is_never_signalled(tmp_path: Path):
    """A live group with no recorded member left is not provably this dispatch's.

    ``probe_group_liveness`` now answers alive for such a group (the removal
    guard must keep the tree), but teardown must still signal a group only
    when a *recorded member* carries the PGID — otherwise a reused PGID would
    turn the fix into a kill of a stranger's process group.
    """
    from dispatch_process_registry import kill_dispatch_pgids

    main = tmp_path / "main"
    main.mkdir()
    proc, child_pid, child_pgid = _spawn_sleeping_child(main)
    try:
        _write_registry(tmp_path, "dispatch-reused-pgid", {child_pgid: [_reaped_pid()]})
        killed = kill_dispatch_pgids("dispatch-reused-pgid", repo_root=tmp_path, grace=0.0)
        assert killed == 0, "no recorded member is left, so nothing may be signalled"
        assert _pid_alive(child_pid), "an unrelated group sharing the PGID must survive"
    finally:
        _reap(proc)
