"""worktree_process_cleanup.py — Kill processes running inside a worktree before teardown.

Without this, a SessionStart hook spawned from a dispatch worktree survives the
dispatch's teardown: the background subshell and its python child keep their CWD
and open file handles inside the soon-to-be-removed worktree, and the python
child can hold the coordination DB write lock indefinitely, blocking every
fleet-wide track write (OI-873, measured 2026-07-31).

Kills by process GROUP (os.killpg), not individual pid, so the child process
that actually holds the DB lock is cleaned up together with its bash parent.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def kill_worktree_processes(wt_path: Path) -> int:
    """Kill process groups with files open or CWD inside *wt_path*.

    Uses ``lsof +D`` to find processes referencing the worktree tree.  Groups
    PIDs by process group (PGID) and sends SIGTERM then, after a short grace
    period, SIGKILL to each group.  Skips the calling process's own group.

    Returns the number of process groups signalled.
    """
    wt_str = str(wt_path.resolve())
    pids: set[int] = set()

    # lsof +D finds every process that has ANY file open under the tree.
    # This catches both the bash parent (CWD inside the worktree) and the
    # python child (coordination DB file handle open inside the worktree).
    try:
        result = subprocess.run(
            ["lsof", "-F", "p", "+D", wt_str],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith("p"):
                try:
                    pids.add(int(line[1:]))
                except ValueError:
                    pass
    except subprocess.TimeoutExpired:
        logger.warning(
            "kill_worktree_processes: lsof +D timed out for %s — "
            "no processes killed; worktree removal may fail",
            wt_str,
        )
        return 0
    except FileNotFoundError:
        logger.debug("kill_worktree_processes: lsof not available — skipping")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("kill_worktree_processes: lsof failed for %s: %s", wt_str, exc)
        return 0

    if not pids:
        logger.debug("kill_worktree_processes: no processes found in %s", wt_str)
        return 0

    # Group by PGID, skip our own process group.
    own_pgid = os.getpgid(0)
    pgids: set[int] = set()
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
            if pgid != own_pgid:
                pgids.add(pgid)
        except (ProcessLookupError, PermissionError):
            pass

    if not pgids:
        return 0

    # Phase 1: SIGTERM — give processes a chance to clean up.
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    # Brief grace period for SIGTERM handlers to run.
    time.sleep(0.3)

    # Phase 2: SIGKILL — any process still alive after grace gets force-killed.
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # Already gone (or only a zombie left) — that's fine.

    logger.info(
        "kill_worktree_processes: signalled %d process group(s) for %s",
        len(pgids),
        wt_str,
    )
    return len(pgids)
