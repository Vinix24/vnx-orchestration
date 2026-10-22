"""One review-gate run per PR head (R10, track ``review-stopregel-en-lichte-stoelen``).

Measured over 15-22 October 2026 (``claudedocs/2026-10-22-codex-druk.md``,
section "Dubbele runs"): 272 codex review sessions ran against 224 distinct
(PR, head) pairs, 48 extra runs. Two mechanisms, both closed here:

- **No retry cap.** Obligation ``20261021-release-v1-7-3`` (PR #2151, head
  ``24412e26``) reached ``attempts = 12``: every launchd cycle ran codex to the
  end and the verdict was refused as ``gate_execution_degenerate``. Nothing
  stopped a run on the same head.
- **Two runners, one head.** On 22-10 the launchd runner started a codex run on
  PR #2170 head ``83bad6a0`` while a manually started runner was still
  reviewing that head. Nothing kept the second launch off.

Two guards, one module:

- **The in-flight lock.** Every launch of a review-gate run takes an exclusive,
  non-blocking ``fcntl.flock`` on one lock file per (project, PR, gate, head
  SHA) under ``<state_dir>/review_gates/run_locks/`` and holds it for the whole
  run (:func:`acquire`). A launch that finds the lock held does not run: it
  logs one line naming the key and the holder's pid and returns a refused
  :class:`RunLock`. The lock belongs to an open file description, so the kernel
  releases it the instant its holder exits, a crash included: no stale lock can
  block a head. Within one process the same thread may take a key it already
  holds (the obligation runner holds it around ``request_and_execute``, whose
  ``execute_gate`` takes it again); another thread of the same process is
  refused like another process.
- **The retry cap per head.** One obligation gets at most
  :data:`MAX_REVIEWER_RUNS_PER_HEAD` runs on one head in which the reviewer
  actually ran (:func:`reviewer_ran`): the first, and one retry after a
  ``gate_execution_degenerate``, a timeout or a stall. A refusal that ran no
  reviewer (quota, capacity before start, a failed setup step) does not count.
  The count lives on the obligation record (``head_runs``) and starts again on
  a new head. The obligation runner applies it (``gate_obligation_runner``).

Lock files never accumulate without bound. A clean release unlinks the file
while it still holds the lock; a taker that opened the old file before that
unlink sees the inode change after it locked and tries again on the new file
(:func:`_lock_file`), so an unlink never lets two holders in. A crash leaves
the file behind, unlocked: :func:`sweep_run_locks` removes every lock file no
one holds, and the obligation runner calls it on every write run.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess in this module.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from gate_obligations import REASON_DEGENERATE_RETRY_CAP

logger = logging.getLogger(__name__)

RUN_LOCKS_SUBDIR = Path("review_gates") / "run_locks"
LOCK_SUFFIX = ".lock"

# What a refused launch reports instead of a verdict: never a pass, never written.
IN_FLIGHT_STATUS = "in_flight"
IN_FLIGHT_REASON = "gate_run_in_flight"
LOCK_ERROR_REASON = "gate_run_lock_unavailable"

# The obligation state at the cap: still pending (the merge door still sees no
# review), but the runner does not run the gate again on that head.
REASON_RETRY_CAP = REASON_DEGENERATE_RETRY_CAP
MAX_REVIEWER_RUNS_PER_HEAD = 2

# Result reasons for a run in which the reviewer ran to an end that is no
# verdict. A ``partial_review`` status (a pass on a cut diff) is a run too: the
# same head cuts the same diff again. Every other unavailable reason (quota,
# lane_exhausted, binary_not_found, worktree_checkout_failed, ...) ran no
# reviewer and does not count.
REVIEWER_RAN_REASONS = frozenset({"gate_execution_degenerate", "timeout", "stall", "stalled"})
_PARTIAL_REVIEW_STATUS = "partial_review"

FORCE_RERUN_USAGE = '--force-rerun --reason "..."'

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_UNKNOWN_HEAD = "unknown-head"
_NO_PROJECT = "_"
# A taker whose file was replaced between its open and its lock tries again on
# the new file. Three times in a row means another process keeps replacing it:
# the head is busy.
_ACQUIRE_ATTEMPTS = 3
_HOLDER_READ_BYTES = 4096

# (lock path, thread id) -> [fd, depth]: the keys this process holds.
_HELD: Dict[Tuple[str, int], List[int]] = {}
_HELD_GUARD = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(part: Any) -> str:
    return _UNSAFE_KEY_CHARS.sub("_", str(part)) or "_"


def run_locks_dir(state_dir: Path) -> Path:
    """``<state_dir>/review_gates/run_locks``: one lock file per (project, PR, gate, head) in flight."""
    return Path(state_dir) / RUN_LOCKS_SUBDIR


def run_lock_path(state_dir: Path, *, pr_ref: Any, gate: str, head_sha: str) -> Path:
    """The lock file of one gate run: ``<project>--pr<ref>--<gate>--<head>.lock``.

    The project is the one the store names itself as (ADR-007), so two
    projects that share a store never share a key. An unknown head is a key of
    its own: two launches that both could not read the head still exclude each
    other.
    """
    import vnx_paths

    project = vnx_paths.project_id_from_state_dir(Path(state_dir)) or _NO_PROJECT
    parts = (project, f"pr{pr_ref}", gate, head_sha or _UNKNOWN_HEAD)
    return run_locks_dir(state_dir) / ("--".join(_safe(part) for part in parts) + LOCK_SUFFIX)


@dataclass
class RunLock:
    """The answer to one launch: ``acquired``, or refused with the holder's pid (or an error)."""

    path: Path
    gate: str
    pr_ref: str
    head_sha: str
    acquired: bool
    holder_pid: Optional[int] = None
    error: str = ""
    _token: Optional[Tuple[str, int]] = field(default=None, repr=False)

    @property
    def key(self) -> str:
        return self.path.name[: -len(LOCK_SUFFIX)]

    @property
    def reason(self) -> str:
        return LOCK_ERROR_REASON if self.error else IN_FLIGHT_REASON

    def refusal_text(self) -> str:
        if self.error:
            return f"run lock {self.key} could not be taken ({self.error}): {self.gate} not launched, no attempt counted"
        holder = f"pid {self.holder_pid}" if self.holder_pid else "a holder whose pid could not be read"
        return (
            f"{self.gate} on PR #{self.pr_ref} head {(self.head_sha or _UNKNOWN_HEAD)[:12]} is already in flight "
            f"(lock {self.key}, held by {holder}): not launched, no attempt counted"
        )

    def release(self) -> None:
        """Give the lock back; the last release in this thread unlinks the file and closes it."""
        token, self._token = self._token, None
        if token is not None:
            _release(token)


def acquire(state_dir: Path, *, pr_ref: Any, gate: str, head_sha: str) -> RunLock:
    """Take the run lock of (PR, gate, head) without waiting; a refusal is logged once, here.

    The same thread may take a key it holds already (the depth goes up); any
    other holder, another process or another thread, refuses the launch.
    """
    path = run_lock_path(state_dir, pr_ref=pr_ref, gate=gate, head_sha=head_sha)
    base = {"path": path, "gate": gate, "pr_ref": str(pr_ref), "head_sha": head_sha or ""}
    token = (str(path), threading.get_ident())
    with _HELD_GUARD:
        held = _HELD.get(token)
        if held is not None:
            held[1] += 1
            return RunLock(**base, acquired=True, holder_pid=os.getpid(), _token=token)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, holder = _lock_file(path)
    except OSError as exc:
        lock = RunLock(**base, acquired=False, error=str(exc))
        logger.warning("gate_run_guard: %s", lock.refusal_text())
        return lock
    if fd is None:
        lock = RunLock(**base, acquired=False, holder_pid=holder)
        logger.warning("gate_run_guard: %s", lock.refusal_text())
        return lock
    _stamp_holder(fd, path)
    with _HELD_GUARD:
        _HELD[token] = [fd, 1]
    return RunLock(**base, acquired=True, holder_pid=os.getpid(), _token=token)


@contextmanager
def held_run(state_dir: Path, *, pr_ref: Any, gate: str, head_sha: str) -> Iterator[RunLock]:
    """:func:`acquire` for a ``with`` block; the lock is released when the block ends, also on an exception."""
    lock = acquire(state_dir, pr_ref=pr_ref, gate=gate, head_sha=head_sha)
    try:
        yield lock
    finally:
        lock.release()


def _lock_file(path: Path) -> Tuple[Optional[int], Optional[int]]:
    """``(fd, None)`` with the lock held, or ``(None, holder_pid)`` when someone else holds it."""
    for _attempt in range(_ACQUIRE_ATTEMPTS):
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _holder_pid(fd)
            os.close(fd)
            return None, holder
        except OSError:
            os.close(fd)
            raise
        if _still_at(path, fd):
            return fd, None
        # The previous holder unlinked this file after our open: lock the new one.
        os.close(fd)
    return None, None


def _still_at(path: Path, fd: int) -> bool:
    """Is ``fd`` still the file at ``path``? A released lock is unlinked, and its path may hold a new file."""
    try:
        on_path = os.stat(str(path))
    except FileNotFoundError:
        return False
    mine = os.fstat(fd)
    return (mine.st_dev, mine.st_ino) == (on_path.st_dev, on_path.st_ino)


def _stamp_holder(fd: int, path: Path) -> None:
    """Write who holds the lock into the lock file, for the refusal of the next taker."""
    data = json.dumps({"pid": os.getpid(), "key": path.name, "acquired_at": _utc_now()}).encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, data, 0)
    except OSError as exc:
        logger.warning("gate_run_guard: holder of %s not written (%s); a refusal names no pid", path.name, exc)


def _holder_pid(fd: int) -> Optional[int]:
    """The pid the holder wrote, ``None`` when it wrote none yet or it cannot be read."""
    try:
        raw = os.pread(fd, _HOLDER_READ_BYTES, 0)
        pid = json.loads(raw.decode("utf-8")).get("pid") if raw else None
    except (OSError, ValueError, AttributeError):
        return None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _release(token: Tuple[str, int]) -> None:
    with _HELD_GUARD:
        held = _HELD.get(token)
        if held is None:
            return
        held[1] -= 1
        if held[1] > 0:
            return
        del _HELD[token]
    fd = held[0]
    path = Path(token[0])
    try:
        # Unlinked while still held: a taker that opened this file meanwhile
        # locks it after the close below, sees the inode gone and takes the new one.
        if _still_at(path, fd):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(str(path))
    finally:
        os.close(fd)


def sweep_run_locks(state_dir: Path) -> List[str]:
    """Remove every run-lock file no one holds; returns the names removed.

    A clean release removes its own file. What is left is the file of a holder
    that died (the kernel released its lock, the file stayed). A file someone
    holds is skipped; a file is only removed while this sweep holds its lock and
    it is still the file at that path, the same rule a release follows.
    """
    directory = run_locks_dir(state_dir)
    removed: List[str] = []
    if not directory.is_dir():
        return removed
    for path in sorted(directory.glob(f"*{LOCK_SUFFIX}")):
        try:
            fd = os.open(str(path), os.O_RDWR)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if _still_at(path, fd):
                os.unlink(str(path))
                removed.append(path.name)
        finally:
            os.close(fd)
    return removed


def in_flight_payload(lock: RunLock, *, pr_number: Optional[int], pr_id: str) -> Dict[str, Any]:
    """What a refused launch returns in place of a result: no verdict, never a pass, never written."""
    return {
        "gate": lock.gate,
        "pr_number": pr_number,
        "pr_id": pr_id,
        "status": IN_FLIGHT_STATUS,
        "reason": lock.reason,
        "reason_detail": lock.refusal_text(),
        "holder_pid": lock.holder_pid,
        "run_lock": str(lock.path),
        "commit_sha": lock.head_sha,
    }


# ---------------------------------------------------------------------------
# The retry cap per head
# ---------------------------------------------------------------------------


def reviewer_ran(result: Optional[Dict[str, Any]]) -> bool:
    """Did the reviewer run in this result, without reaching a verdict the obligation can book?"""
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "").strip().lower() == _PARTIAL_REVIEW_STATUS:
        return True
    return str(result.get("reason") or "") in REVIEWER_RAN_REASONS


def _run_count(head_runs: Any) -> int:
    count = head_runs.get("reviewer_runs") if isinstance(head_runs, dict) else 0
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def runs_on_head(head_runs: Any, head_sha: str) -> int:
    """Reviewer runs the obligation recorded on ``head_sha``; a record about another head counts nothing."""
    if not head_sha or not isinstance(head_runs, dict) or head_runs.get("head_sha") != head_sha:
        return 0
    return _run_count(head_runs)


def next_head_runs(head_runs: Any, head_sha: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """``head_runs`` after one more reviewer run on ``head_sha``: a new head starts at one."""
    return {
        "head_sha": head_sha,
        "reviewer_runs": runs_on_head(head_runs, head_sha) + 1,
        "last_status": str(result.get("status") or ""),
        "last_reason": str(result.get("reason") or ""),
        "last_run_at": _utc_now(),
    }


def cap_reached(head_runs: Any, head_sha: str) -> bool:
    """Has the obligation used its runs on ``head_sha``? An unknown head is never capped."""
    return runs_on_head(head_runs, head_sha) >= MAX_REVIEWER_RUNS_PER_HEAD


def cap_detail(gate: str, pr_number: Any, dispatch_id: str, head_runs: Dict[str, Any]) -> str:
    """The reason detail of an obligation at the cap: what ran, on which head, and the two ways on."""
    head = str(head_runs.get("head_sha") or "")
    last = str(head_runs.get("last_reason") or head_runs.get("last_status") or "no verdict")
    return (
        f"{REASON_RETRY_CAP}: {gate} PR #{pr_number} head {head[:8]} ran {_run_count(head_runs)} times "
        f"without a verdict (last: {last}); the runner does not run it again on this head. A new head "
        f"resets the count; an operator rerun is gate_obligation_runner.py --dispatch-prefix {dispatch_id} "
        f"{FORCE_RERUN_USAGE}"
    )


def force_rerun_record(reason: str, *, head_sha: str, set_by: str) -> Dict[str, Any]:
    """The record an operator force-rerun leaves on the obligation, the request and the result."""
    return {"reason": reason.strip(), "head_sha": head_sha, "set_by": set_by, "at": _utc_now()}
