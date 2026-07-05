"""dispatch_serialization.py — Claude subscription N-slot serial lock (PR-6 + tmux-concurrency-config).

serialize_lane(serialization_class) context manager: serializes the claude-tmux
lane to at most N concurrent holders per account; provider + headless lanes
pass None -> no-op.

The serial lock protects the Claude SUBSCRIPTION, not a resource. Running
multiple subscription-authenticated `claude` processes concurrently risks
rate-limits and (per prior-incident precedent) account action. Default
concurrency is 1 (fully serial, the historically-safe behavior). An operator
who accepts that trade-off may opt in to more headroom via
VNX_TMUX_MAX_CONCURRENT — e.g. =3 to run three tmux-spawn workers at once.
This is an explicit, informed opt-in: raising it is the operator's call, not
a default the code should creep towards.

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
from typing import List, Optional

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


def _max_concurrent() -> int:
    """N-slot concurrency limit for the claude-tmux lane.

    VNX_TMUX_MAX_CONCURRENT, clamped to >= 1. Missing, unparseable, zero, or
    negative values fall back to 1 -- the subscription-safe default. Only a
    valid positive integer opts into more than one concurrent slot.
    """
    raw = os.environ.get("VNX_TMUX_MAX_CONCURRENT", "1")
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return 1
    return n if n >= 1 else 1


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Slot path helpers
# ---------------------------------------------------------------------------

def _slot_lock_paths(lock_dir: Path, serialization_class: str, n: int) -> List[Path]:
    """N per-slot lock file paths: <lock_dir>/<class>-slot-<0..n-1>.lock."""
    return [lock_dir / f"{serialization_class}-slot-{i}.lock" for i in range(n)]


def _slot_glob_pattern(serialization_class: str) -> str:
    return f"{serialization_class}-slot-*.lock"


def _describe_holder(lock_path: Path) -> str:
    try:
        raw = lock_path.read_text(encoding="utf-8")
        holder = json.loads(raw)
        return (
            f"{lock_path.name}: pid={holder.get('pid')}, "
            f"dispatch_id={holder.get('dispatch_id')!r}, "
            f"since={holder.get('timestamp')}"
        )
    except Exception:
        return f"{lock_path.name}: unknown holder (lock file unreadable)"


# ---------------------------------------------------------------------------
# Lock acquisition with wait-warn
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 0.2  # seconds between LOCK_NB retries


def _try_acquire_any_slot(fds: List[int]) -> Optional[int]:
    """Try each slot fd non-blocking in turn; return the index of the first
    slot successfully locked, or None if all slots are currently held.

    Any OSError whose errno is NOT a contention errno (e.g. EBADF) is a real
    fd error and is re-raised immediately rather than treated as "busy".
    """
    for idx, fd in enumerate(fds):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return idx
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
    return None


def _acquire_any_slot_with_warn(
    fds: List[int],
    lock_paths: List[Path],
    serialization_class: str,
    dispatch_id: Optional[str],
) -> int:
    """Acquire the first free slot among fds, with wait-warn and optional
    hard timeout. Polls ALL slots each interval (not just slot 0) so whichever
    holder releases first is grabbed immediately.
    """
    warn_secs = _warn_seconds()
    timeout_secs = _timeout_seconds()
    start = time.monotonic()
    warned = False

    while True:
        idx = _try_acquire_any_slot(fds)
        if idx is not None:
            return idx

        elapsed = time.monotonic() - start

        if not warned and elapsed >= warn_secs:
            holder_info = "; ".join(_describe_holder(p) for p in lock_paths)
            logger.warning(
                "[dispatch_serialization] WAITING for a free %s slot "
                "(all %d busy) — %s (%.0fs elapsed) — still waiting; "
                "use --force-release-lock to clear a stale lock",
                serialization_class,
                len(fds),
                holder_info,
                elapsed,
            )
            warned = True

        if timeout_secs > 0 and elapsed >= timeout_secs:
            raise TimeoutError(
                f"{serialization_class} serial lock: no free slot within "
                f"{timeout_secs:.0f}s (elapsed {elapsed:.0f}s, "
                f"{len(fds)} slot(s) all busy); "
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
    """Serialize execution for the given serialization_class across N slots.

    None -> no-op: yield immediately, touch nothing (providers + headless).
    "claude-tmux" -> acquire the first free slot among N exclusive flocks on
    <lock_dir>/claude-tmux-slot-{0..N-1}.lock (N = VNX_TMUX_MAX_CONCURRENT,
    default 1) and hold it through the entire with-body (execution +
    receipt/GOVERN). The acquired slot is released unconditionally in
    finally, including on exception. flock auto-releases on process death;
    no manual stale-lock cleanup needed.

    Note: flock is NOT reentrant in the same process for the same inode. A
    nested serialize_lane("claude-tmux") within the same process can self
    -deadlock once all N slots are exhausted by the outer call(s). This is
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

    n = _max_concurrent()
    lock_paths = _slot_lock_paths(lock_dir, serialization_class, n)

    fds: List[int] = []
    try:
        for lock_path in lock_paths:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)  # tighten existing file if it had lax permissions
            fds.append(fd)

        idx = _acquire_any_slot_with_warn(fds, lock_paths, serialization_class, dispatch_id)

        # Write holder metadata for diagnostics + wait-warn + force-release.
        # Written AFTER acquiring so only the true current holder is recorded.
        metadata = {
            "pid": os.getpid(),
            "dispatch_id": dispatch_id,
            "timestamp": _iso_now(),
        }
        os.ftruncate(fds[idx], 0)
        os.lseek(fds[idx], 0, os.SEEK_SET)
        os.write(fds[idx], json.dumps(metadata).encode("utf-8"))

        try:
            yield
        finally:
            fcntl.flock(fds[idx], fcntl.LOCK_UN)
    finally:
        for fd in fds:
            os.close(fd)


def force_release(serialization_class: str = "claude-tmux") -> None:
    """Operator escape: print holder metadata and remove ALL slot lock files
    for this class (glob <lock_dir>/<class>-slot-*.lock).

    Every matching slot file is inspected independently. For each:
    - If holder ALIVE: prints a LOUD double-run warning and proceeds with removal.
      A new acquire on a fresh inode can then run concurrently with the
      original holder — true parallel double-run. Only use when the holder
      is genuinely hung and not making progress.
    - If holder DEAD: notes the holder is gone (flock auto-released) and removal is safe.
    - If pid unreadable: removes without liveness check.

    Does NOT kill any holder process. After removal, new acquires succeed immediately.

    WARNING: force-releasing a LIVE holder allows another claude-tmux dispatch to run
    concurrently (double-run). The flock on the old (now-unlinked) inode remains held
    by the original process while a new dispatch acquires a lock on a fresh inode.
    """
    lock_dir = _lock_dir()
    pattern = _slot_glob_pattern(serialization_class)
    slot_paths = sorted(lock_dir.glob(pattern))

    if not slot_paths:
        print(f"[force-release] No lock files found matching: {lock_dir / pattern}")
        print("[force-release] No stale lock to clear.")
        return

    for lock_path in slot_paths:
        _force_release_one(lock_path)


def _force_release_one(lock_path: Path) -> None:
    holder_pid = None
    try:
        raw = lock_path.read_text(encoding="utf-8")
        holder = json.loads(raw)
        holder_pid = holder.get("pid")
        print(f"[force-release] Prior holder metadata ({lock_path.name}): {holder}")
        print(f"[force-release]   pid          = {holder.get('pid')}")
        print(f"[force-release]   dispatch_id  = {holder.get('dispatch_id')}")
        print(f"[force-release]   timestamp    = {holder.get('timestamp')}")
    except Exception as exc:
        print(f"[force-release] Could not read holder metadata ({lock_path.name}): {exc}")

    if holder_pid is not None:
        try:
            os.kill(holder_pid, 0)
            # No exception -> process is alive
            print(
                f"WARNING: holder pid {holder_pid} ({lock_path.name}) is STILL ALIVE. "
                "Force-releasing now will let a SECOND claude-tmux dispatch run "
                "in PARALLEL with it (double-run). Only do this if that process "
                "is hung/not-progressing."
            )
        except ProcessLookupError:
            print(f"[force-release] Holder pid {holder_pid} is already gone (safe to release).")
        except PermissionError:
            # Process exists but belongs to another user
            print(
                f"WARNING: holder pid {holder_pid} ({lock_path.name}) is STILL ALIVE "
                "(owned by another user). Force-releasing now will let a SECOND "
                "claude-tmux dispatch run in PARALLEL with it (double-run). Only do "
                "this if that process is hung/not-progressing."
            )

    lock_path.unlink(missing_ok=True)
    print(f"[force-release] Lock file removed: {lock_path}")
    print(
        "[force-release] NOTE: flock auto-releases on holder process death. "
        "If the holder was alive, removing the lock allows a new dispatch to acquire "
        "a fresh inode lock — running concurrently with the original holder (double-run). "
        "Force-release is only safe when the holder is hung and not progressing."
    )
