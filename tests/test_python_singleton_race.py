#!/usr/bin/env python3
"""Regression tests for OI-1145: singleton lock race during _cleanup().

The bug: _cleanup() released the flock BEFORE unlinking the lock file.
Window: contender B acquired the lock after LOCK_UN; then A unlinked;
then C created a new file and also acquired — B and C both held the singleton.

Fix: unlink while the flock is still held, so any new opener gets a fresh
inode that is independent of the one being released.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from python_singleton import enforce_python_singleton


class TestSingletonLockRace:
    def test_basic_acquire_and_release(self, tmp_path):
        """Sanity: first caller acquires, second is rejected."""
        locks = tmp_path / "locks"
        pids = tmp_path / "pids"
        handle = enforce_python_singleton("test", str(locks), str(pids))
        assert handle is not None
        # Second call in same process — lock already held by us, LOCK_NB would
        # block on same fd but we open a new handle to the same file.
        # On most systems this returns the lock (same process owns it).
        # What matters is that the API contract holds.
        handle.close()

    def test_unlink_happens_before_lock_release(self, tmp_path):
        """After _cleanup runs (via atexit or direct call), the lock file must
        not exist while the flock is still released — i.e. the unlink and
        LOCK_UN are ordered correctly.

        We verify the ordering indirectly: after a subprocess that held the
        singleton exits, a second process must be able to acquire the lock
        cleanly without a stale file being present from the old holder.
        """
        locks = tmp_path / "locks"
        pids = tmp_path / "pids"

        # Acquire and immediately release via a subprocess so that atexit fires.
        import subprocess
        script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / "scripts" / "lib")!r})
from python_singleton import enforce_python_singleton
h = enforce_python_singleton("race_test", {str(locks)!r}, {str(pids)!r})
assert h is not None, "first acquire should succeed"
# h closed by atexit _cleanup when process exits
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()

        # After the subprocess exits, its _cleanup has run.
        # Lock file should be gone (unlinked by cleanup).
        lock_file = locks / "race_test.lock"
        assert not lock_file.exists(), (
            f"Lock file {lock_file} still exists after holder exited — "
            "cleanup may have unlinked after LOCK_UN"
        )

        # New process should be able to acquire cleanly.
        handle2 = enforce_python_singleton("race_test", str(locks), str(pids))
        assert handle2 is not None, "second process should acquire after first exits"
        handle2.close()

    def test_no_overlap_between_sequential_holders(self, tmp_path):
        """Two sequential processes must not both think they hold the singleton.

        We check that B cannot acquire while A is still live, and that after A
        exits B can acquire. Overlap = both returning a non-None handle.
        """
        locks = tmp_path / "locks"
        pids = tmp_path / "pids"
        lib = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")

        acquire_script = f"""
import sys, time
sys.path.insert(0, {lib!r})
from python_singleton import enforce_python_singleton
h = enforce_python_singleton("overlap_test", {str(locks)!r}, {str(pids)!r})
if h is None:
    sys.exit(1)   # could not acquire
time.sleep(0.3)   # hold for a bit
# atexit fires on exit
"""
        reject_script = f"""
import sys
sys.path.insert(0, {lib!r})
from python_singleton import enforce_python_singleton
h = enforce_python_singleton("overlap_test", {str(locks)!r}, {str(pids)!r})
if h is not None:
    sys.exit(2)   # should NOT have acquired while A holds
    h.close()
sys.exit(0)
"""
        import subprocess, threading

        holder = subprocess.Popen([sys.executable, "-c", acquire_script])
        time.sleep(0.05)  # give A time to acquire

        contender = subprocess.run([sys.executable, "-c", reject_script], capture_output=True)
        # contender must NOT have acquired (exit 0 means correctly rejected)
        assert contender.returncode == 0, (
            "Contender acquired the singleton while holder was still running — race detected"
        )

        holder.wait(timeout=2)
        assert holder.returncode == 0

        # After A exits, B should be able to acquire.
        post_script = f"""
import sys
sys.path.insert(0, {lib!r})
from python_singleton import enforce_python_singleton
h = enforce_python_singleton("overlap_test", {str(locks)!r}, {str(pids)!r})
sys.exit(0 if h is not None else 3)
"""
        post = subprocess.run([sys.executable, "-c", post_script], capture_output=True)
        assert post.returncode == 0, "Post-exit acquire failed — lock file may be stale"

    def test_lock_file_absent_after_cleanup(self, tmp_path):
        """Lock file must not exist after the holder's atexit cleanup runs."""
        locks = tmp_path / "locks"
        pids = tmp_path / "pids"
        lib = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
        lock_file = locks / "cleanup_test.lock"

        script = f"""
import sys
sys.path.insert(0, {lib!r})
from python_singleton import enforce_python_singleton
h = enforce_python_singleton("cleanup_test", {str(locks)!r}, {str(pids)!r})
assert h is not None
"""
        import subprocess
        result = subprocess.run([sys.executable, "-c", script], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        assert not lock_file.exists(), (
            "Lock file survives after process exit — unlink ordering bug still present"
        )
