#!/usr/bin/env python3
"""Lightweight singleton guard for long-running Python daemons."""

from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path
from typing import Optional, TextIO, Callable


def enforce_python_singleton(
    name: str,
    locks_dir: str,
    pids_dir: str,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[TextIO]:
    """Acquire a non-blocking singleton lock.

    Returns an open lock handle when the lock is acquired.
    Returns None when another instance is already running.
    """

    lock_dir = Path(locks_dir)
    pid_dir = Path(pids_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    pid_dir.mkdir(parents=True, exist_ok=True)

    lock_path = lock_dir / f"{name}.lock"
    pid_path = pid_dir / f"{name}.pid"

    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            existing_pid = pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            existing_pid = "unknown"
        if log:
            log(f"[SINGLETON] {name} already running (PID: {existing_pid})")
        lock_handle.close()
        return None

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    if log:
        log(f"[SINGLETON] Lock acquired for {name} (PID: {os.getpid()})")

    def _cleanup() -> None:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_handle.close()
        except OSError:
            pass
        try:
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_cleanup)
    return lock_handle
