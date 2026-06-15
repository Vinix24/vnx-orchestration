"""dispatch_serialization.py — Claude subscription serial lock (PR-6).

serialize_lane(serialization_class) context manager: serializes the claude-tmux
lane (one at a time per account); provider + headless lanes pass None -> no-op.

Lock is account-level: $VNX_LOCK_DIR or ~/.vnx-data/locks — shared across all
projects and worktrees that use the same Claude subscription.

Posix-only: requires fcntl (unavailable on Windows).
"""
from __future__ import annotations

import datetime
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
    return datetime.datetime.utcnow().isoformat() + "Z"


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
        except OSError:
            pass

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
    lock_path = lock_dir / f"{serialization_class}.lock"

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
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

    Does NOT kill the holder process — prints pid + dispatch_id so the operator
    can act. After removal, a new acquire can succeed immediately.

    Note: flock auto-releases on holder process death, so force-release is only
    needed when a holder process is hung (not dead, not making progress).
    """
    lock_dir = _lock_dir()
    lock_path = lock_dir / f"{serialization_class}.lock"

    if not lock_path.exists():
        print(f"[force-release] No lock file found: {lock_path}")
        print("[force-release] No stale lock to clear.")
        return

    try:
        raw = lock_path.read_text(encoding="utf-8")
        holder = json.loads(raw)
        print(f"[force-release] Prior holder metadata: {holder}")
        print(f"[force-release]   pid          = {holder.get('pid')}")
        print(f"[force-release]   dispatch_id  = {holder.get('dispatch_id')}")
        print(f"[force-release]   timestamp    = {holder.get('timestamp')}")
    except Exception as exc:
        print(f"[force-release] Could not read holder metadata: {exc}")

    lock_path.unlink(missing_ok=True)
    print(f"[force-release] Lock file removed: {lock_path}")
    print(
        "[force-release] NOTE: flock auto-releases on holder process death. "
        "Force-release is only needed for a hung holder (process alive, not progressing)."
    )
