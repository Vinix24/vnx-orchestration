"""dispatch_serialization.py — Claude subscription N-slot serial lock (PR-6 + tmux-concurrency-config).

serialize_lane(serialization_class) context manager: serializes BOTH claude
lanes (claude-tmux and claude_headless) to at most N concurrent holders per
account, since they authenticate against the same subscription (OI-1417) —
provider lanes pass None -> no-op.

The serial lock protects the Claude SUBSCRIPTION, not a resource. Running
multiple subscription-authenticated `claude` processes concurrently risks
rate-limits and (per prior-incident precedent) account action. Default
concurrency is 10 (operator directive 2026-08-21, dispatch-20260821-t0-tmux-
concurrency-10; raised from 5 -- operator directive 2026-08-11,
dispatch-20260811c-b). Concurrent tmux dispatches used to cross each other's instructions through
tmux's single shared paste buffer — measured 15 of 20 crossings with 4
simultaneous dispatches. #1451 gave every dispatch its own named paste
buffer; the same measurement came back 0 of 20 crossings after. Raising the
default past 1 is only sound on a fabric that carries that fix. An operator
may still dial concurrency up or down via VNX_TMUX_MAX_CONCURRENT — e.g. =8
for more headroom, or =1 to go back to fully serial; this is an explicit,
informed choice, not a default the code should creep towards on its own.

Lock is account-level: $VNX_LOCK_DIR or ~/.vnx-data/locks — shared across ALL
projects and worktrees that use the same Claude subscription, by design: the
subscription session cap is an account property, not a per-project one.
Setting VNX_LOCK_DIR per project to get 5 slots per project (instead of 5
across the whole account) multiplies past the real limit — don't.

Lock-wait timeout (VNX_CLAUDE_LOCK_TIMEOUT_SECONDS) is FINITE by default
(dispatch-20260904-lock-timeout-eindig, OI/gap measured 2026-09-04): the
default used to be "0" and the wait-loop's `if timeout_secs > 0` guard made
the TimeoutError branch structurally unreachable at that default — a worker
waiting on a saturated lane (all N slots busy) polled forever with only a
one-time 60s log warning, no matter how long the wait ran. See
_DEFAULT_TIMEOUT_SECONDS below for the grounding of the new default. `0`
remains a legal, explicit opt-out ("wait forever") via
VNX_CLAUDE_LOCK_TIMEOUT_SECONDS=0 for an operator who deliberately accepts
that trade-off — it is no longer the silent default a caller falls into by
doing nothing.

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


# Default lock-wait timeout in seconds (VNX_CLAUDE_LOCK_TIMEOUT_SECONDS), finite
# by design (dispatch-20260904-lock-timeout-eindig).
#
# Grounded on the documented ceiling for how long any ONE slot-holder may
# legitimately occupy a slot: dispatch_spec.DEADLINE_SECONDS_MAX = 14400 (4h)
# is the enforced upper bound on a dispatch's own deadline_seconds
# (scripts/lib/dispatch_spec.py, validate() Rule 11 + the door's
# --deadline-seconds bound), and serialize_lane holds its acquired slot for
# the holder's entire execution + receipt/GOVERN write (see module docstring
# above) — a legitimate holder can never occupy a slot past that ceiling.
# So under worst-case *legitimate* saturation (all N slots freshly occupied
# by max-deadline dispatches), the longest a well-behaved waiter should ever
# need to queue for a first free slot is just under that same ceiling. This
# is a designed bound, not an empirical median: checked on 2026-09-04 across
# every retained ~/.vnx-data / <repo>/.vnx-data tree on this machine for a
# genuinely-fired 60s wait-warn log line (the WARNING at line ~205 below) --
# every hit found was the literal source text of this file re-read into a
# past agent transcript/DB blob, not an actual runtime emission, so there is
# no local telemetry an empirical default could be measured against instead.
# Waiting longer than the dispatch-deadline ceiling is not "busy", it is
# stuck (a holder whose process died without releasing flock in a way that
# still leaves the slot file looking held, a runaway process past its own
# deadline that never tore down, etc.) and should fail loud well before
# "hangs until the next morning" (8h+), not after.
_DEFAULT_TIMEOUT_SECONDS = 14400.0  # 4h — see dispatch_spec.DEADLINE_SECONDS_MAX


def _timeout_seconds() -> float:
    """Lock-wait timeout in seconds, from VNX_CLAUDE_LOCK_TIMEOUT_SECONDS.

    Unset -> _DEFAULT_TIMEOUT_SECONDS (finite). Exactly "0" is a legal,
    explicit opt-out ("wait forever") and is returned as 0.0 verbatim --
    see the module docstring. A negative or unparseable value is not a sane
    explicit choice (there is no meaningful negative wait) and falls back to
    the finite default, same fail-soft-to-default posture as
    _max_concurrent()'s clamp of bad input below.
    """
    raw = os.environ.get("VNX_CLAUDE_LOCK_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value < 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return value


def _max_concurrent() -> int:
    """N-slot concurrency limit for the claude-tmux lane.

    Precedence: the process env var VNX_TMUX_MAX_CONCURRENT (an explicit
    per-session override) wins; then the registry-backed config-plane value
    (project_config via config_runtime -- the same surface an operator's
    dashboard flips, category "dispatch", approval-gated); then 10 -- the
    account-wide default (operator directive 2026-08-21, dispatch-20260821-
    t0-tmux-concurrency-10; raised from 5 since the per-dispatch tmux
    paste-buffer fix, #1451 -- see module docstring). Missing, unparseable,
    zero, or negative values fall back to 10. Only a valid positive integer
    overrides the default with a different concurrency level. The config
    lookup is best-effort (fail-soft): a missing state dir / DB leaves the
    env/default behaviour unchanged, exactly as before this flag was
    registry-backed.
    """
    raw = os.environ.get("VNX_TMUX_MAX_CONCURRENT")
    if not raw:
        try:
            import config_runtime  # noqa: PLC0415
            raw = config_runtime.get("VNX_TMUX_MAX_CONCURRENT")
        except Exception as exc:  # vnx-silent-except: unreadable config -> log + fail-soft to default
            logger.warning(
                "dispatch_serialization: VNX_TMUX_MAX_CONCURRENT config read failed "
                "(falling back to default 10, fail-soft direction): %s", exc
            )
            raw = None
    try:
        n = int(raw) if raw else 10
    except (ValueError, TypeError):
        return 10
    return n if n >= 1 else 10


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


def _clear_holder_metadata(fd: int) -> None:
    """Clear the holder metadata written at acquire time (OI-844).

    Called on every release path (normal and exception) before the flock is
    released, so a freed slot reads as FREE — no pid/dispatch_id/timestamp —
    to force_release() and any diagnostic reader, instead of still showing the
    prior holder as OCCUPIED. Does not touch the lock itself: the flock is the
    sole ownership mechanism and is released separately right after this call.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)


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


class LockWaitTimeout(TimeoutError):
    """No lane slot freed up within the configured lock-wait timeout.

    A dedicated subclass (not a bare TimeoutError) so a future caller can
    catch this specifically and record a `failed_delivery` outcome instead
    of folding it into a generic runtime-error path — still catchable as a
    plain TimeoutError by any existing `except TimeoutError` / `except
    Exception` (subclass semantics).

    KNOWN GAP (dispatch-20260904-lock-timeout-eindig): as of this fix, no
    caller does that yet. The single call site of serialize_lane()
    (scripts/lib/dispatch_cli.py, run_dispatch(), the
    `with serialize_lane(plan.serialization_class, ...)` block) is wrapped
    only by a generic `except Exception as exc: print(...REJECT
    [runtime-error]...); return 1` — it never transitions the dispatch's
    coordination-DB state to `failed_delivery` (that state is only ever
    written from inside the lane executors, e.g.
    headless_adapter.py's `_record_failure`, never from dispatch_cli.py's
    own exception handling). Wiring that classification is out of scope for
    this module and must land in dispatch_cli.py.
    """


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
    """Acquire the first free slot among fds, with wait-warn and a hard
    timeout that is FINITE by default (_DEFAULT_TIMEOUT_SECONDS; opt out of
    it entirely with VNX_CLAUDE_LOCK_TIMEOUT_SECONDS=0 -- see _timeout_seconds()
    and the module docstring). Polls ALL slots each interval (not just slot 0)
    so whichever holder releases first is grabbed immediately. Raises
    LockWaitTimeout when the timeout elapses with no free slot.
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
            raise LockWaitTimeout(
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

    None -> no-op: yield immediately, touch nothing (provider lanes only).
    "claude-tmux" -> acquire the first free slot among N exclusive flocks on
    <lock_dir>/claude-tmux-slot-{0..N-1}.lock (N = VNX_TMUX_MAX_CONCURRENT,
    default 10) and hold it through the entire with-body (execution +
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

    Raises LockWaitTimeout (a TimeoutError subclass) if no slot frees up
    within VNX_CLAUDE_LOCK_TIMEOUT_SECONDS (default _DEFAULT_TIMEOUT_SECONDS,
    finite; VNX_CLAUDE_LOCK_TIMEOUT_SECONDS=0 opts out and waits forever,
    explicitly). See the module docstring for the default's rationale.
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
            _clear_holder_metadata(fds[idx])
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
