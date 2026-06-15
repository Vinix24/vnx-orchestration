"""dispatch_serialization.py — Claude subscription serial lock (PR-6).

serialize_lane(serialization_class) context manager: serializes the claude-tmux
lane (one at a time per account); provider + headless lanes pass None -> no-op.

Lock is account-level: $VNX_LOCK_DIR or ~/.vnx-data/locks — shared across all
projects and worktrees that use the same Claude subscription.

Posix-only: requires fcntl (unavailable on Windows).
"""
from __future__ import annotations

import datetime
import errno
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import fcntl
    _FLOCK_AVAILABLE = True
except ImportError:
    fcntl = None  # type: ignore[assignment]
    _FLOCK_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _lock_dir() -> Path:
    """Account-level lock directory: $VNX_LOCK_DIR or ~/.vnx-data/locks."""
    env = os.environ.get("VNX_LOCK_DIR", "")
    if env:
        return Path(env)
    return Path.home() / ".vnx-data" / "locks"


def _warn_seconds() -> float:
    try:
        return float(os.environ.get("VNX_CLAUDE_LOCK_WAIT_WARN_SECONDS", "60"))
    except (ValueError, TypeError):
        return 60.0


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("VNX_CLAUDE_LOCK_TIMEOUT_SECONDS", "0"))
    except (ValueError, TypeError):
        return 0.0


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Lock acquisition with wait-warn
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 0.2  # seconds between LOCK_NB retries


def _acquire_with_warn(
    fd: int,
    lock_path: Path,
    dispatch_id: Optional[str],
) -> None:
    """Acquire LOCK_EX on fd with wait-warn and optional hard timeout.

    Polls with LOCK_NB so we can emit a warning after warn_seconds and enforce
    an optional hard ceiling without relying on SIGALRM (thread-unsafe).
    """
    warn_secs = _warn_seconds()
    timeout_secs = _timeout_seconds()
    start = time.monotonic()
    warned = False

    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return  # acquired
        except OSError as exc:
            # Only contention errnos mean "still locked — keep polling".
            # Any other errno (e.g. EBADF) is a real fd error; re-raise immediately.
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise

        elapsed = time.monotonic() - start

        if not warned and elapsed >= warn_secs:
            try:
                raw = lock_path.read_text(encoding="utf-8")
                holder = json.loads(raw)
                holder_info = (
                    f"pid={holder.get('pid')}, "
                    f"dispatch_id={holder.get('dispatch_id')!r}, "
                    f"since={holder.get('timestamp')}"
                )
            except Exception:
                holder_info = "unknown holder (lock file unreadable)"
            logger.warning(
                "[dispatch_serialization] WAITING for %s serial lock "
                "— held by %s (%.0fs elapsed) — still waiting; "
                "use --force-release-lock to clear a stale lock",
                lock_path.name,
                holder_info,
                elapsed,
            )
            warned = True

        if timeout_secs > 0 and elapsed >= timeout_secs:
            raise TimeoutError(
                f"claude-tmux serial lock not acquired within {timeout_secs:.0f}s "
                f"(elapsed {elapsed:.0f}s); "
                f"use --force-release-lock to clear a stale lock"
            )

        time.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@contextmanager
def serialize_lane(
    serialization_class: Optional[str],
    *,
    dispatch_id: Optional[str] = None,
):
    """Serialize execution for the given serialization_class.

    None -> no-op: yield immediately, touch nothing (providers + headless).
    "claude-tmux" -> acquire exclusive flock on <lock_dir>/claude-tmux.lock
    and hold it through the entire with-body (execution + receipt/GOVERN).
    Lock is released unconditionally in finally, including on exception.
    flock auto-releases on process death; no manual stale-lock cleanup needed.

    Note: flock is NOT reentrant in the same process for the same inode. A nested
    serialize_lane("claude-tmux") within the same process self-deadlocks. This is
    intentional for VNX's separate-process dispatch model — do not nest.

    Note: advisory flock() over NFS may not serialize across all client/kernel
    combinations. The default lock dir (~/.vnx-data/locks) is local — informational
    only; no action needed unless running lock dir on a network filesystem.
    """
    if not serialization_class:
        yield
        return

    if not _FLOCK_AVAILABLE:
        raise RuntimeError(
            "VNX dispatch lock requires a posix flock (fcntl module not available; "
            "dispatch_serialization is posix-only)"
        )

    lock_dir = _lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_dir.chmod(0o700)  # account-level lock dir must not be other-user writable
    lock_path = lock_dir / f"{serialization_class}.lock"

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)  # tighten existing file if it had lax permissions
        _acquire_with_warn(fd, lock_path, dispatch_id)

        # Write holder metadata for diagnostics + wait-warn + force-release.
        # Written AFTER acquiring so only the true current holder is recorded.
        metadata = {
            "pid": os.getpid(),
            "dispatch_id": dispatch_id,
            "timestamp": _iso_now(),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(metadata).encode("utf-8"))

        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def force_release(serialization_class: str = "claude-tmux") -> None:
    """Operator escape: print holder metadata and remove the lock file.

    Checks whether the holder process is still alive BEFORE removing:
    - If ALIVE: prints a LOUD double-run warning and proceeds with removal.
      A new acquire on a fresh inode can then run concurrently with the
      original holder — true parallel double-run. Only use when the holder
      is genuinely hung and not making progress.
    - If DEAD: notes the holder is gone (flock auto-released) and removal is safe.
    - If pid unreadable: removes without liveness check.

    Does NOT kill the holder process. After removal, a new acquire succeeds immediately.

    WARNING: force-releasing a LIVE holder allows two claude-tmux dispatches to run
    concurrently (double-run). The flock on the old (now-unlinked) inode remains held
    by the original process while a new dispatch acquires a lock on a fresh inode.
    """
    lock_dir = _lock_dir()
    lock_path = lock_dir / f"{serialization_class}.lock"

    if not lock_path.exists():
        print(f"[force-release] No lock file found: {lock_path}")
        print("[force-release] No stale lock to clear.")
        return

    holder_pid = None
    try:
        raw = lock_path.read_text(encoding="utf-8")
        holder = json.loads(raw)
        holder_pid = holder.get("pid")
        print(f"[force-release] Prior holder metadata: {holder}")
        print(f"[force-release]   pid          = {holder.get('pid')}")
        print(f"[force-release]   dispatch_id  = {holder.get('dispatch_id')}")
        print(f"[force-release]   timestamp    = {holder.get('timestamp')}")
    except Exception as exc:
        print(f"[force-release] Could not read holder metadata: {exc}")

    if holder_pid is not None:
        try:
            os.kill(holder_pid, 0)
            # No exception -> process is alive
            print(
                f"WARNING: holder pid {holder_pid} is STILL ALIVE. "
                "Force-releasing now will let a SECOND claude-tmux dispatch run "
                "in PARALLEL with it (double-run). Only do this if that process "
                "is hung/not-progressing."
            )
        except ProcessLookupError:
            print(f"[force-release] Holder pid {holder_pid} is already gone (safe to release).")
        except PermissionError:
            # Process exists but belongs to another user
            print(
                f"WARNING: holder pid {holder_pid} is STILL ALIVE (owned by another user). "
                "Force-releasing now will let a SECOND claude-tmux dispatch run "
                "in PARALLEL with it (double-run). Only do this if that process "
                "is hung/not-progressing."
            )

    lock_path.unlink(missing_ok=True)
    print(f"[force-release] Lock file removed: {lock_path}")
    print(
        "[force-release] NOTE: flock auto-releases on holder process death. "
        "If the holder was alive, removing the lock allows a new dispatch to acquire "
        "a fresh inode lock — running concurrently with the original holder (double-run). "
        "Force-release is only safe when the holder is hung and not progressing."
    )
