"""worktree_removal_guard.py — the one gate before a worktree is removed (PR-4a).

Holds the tree's slot through the removal, keeps it when process liveness is
unknown, and revalidates HEAD + a clean status under the slot before removing.
The removal is bound to the HEAD sha its caller evaluated: a tree that moved
off that sha is refused (``head_moved``), never force-removed.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from gate_worktree import _release_lifetime_lock, _worktree_lock, gate_lifetime_lock_path

log = logging.getLogger(__name__)

REASON_REMOVED = "removed"
REASON_DRY_RUN = "dry_run"
REASON_ABSENT = "absent"
REASON_OCCUPIED = "occupied"
REASON_SLOT_UNAVAILABLE = "slot_unavailable"
REASON_GATE_SEAT_LIVE = "gate_seat_live"
REASON_GATE_SEAT_UNKNOWN = "gate_seat_unknown"
REASON_LIVENESS_ALIVE = "liveness_alive"
REASON_LIVENESS_UNKNOWN = "liveness_unknown"
REASON_DIRTY = "dirty"
REASON_HEAD_MOVED = "head_moved"
REASON_GIT_UNMEASURABLE = "git_unmeasurable"
REASON_REMOVE_FAILED = "remove_failed"
REASON_DECISION_UNRECORDED = "decision_unrecorded"

_GATE_PREFIX = "gate-"


@dataclass(frozen=True)
class GuardResult:
    removed: bool
    reason: str
    detail: str = ""
    dry_run: bool = False
    would_remove: bool = False


def _git(cwd: Path, args: "list[str]") -> "tuple[bool, str]":
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip() or "git failed"
    return True, p.stdout


def _evidence(wt: Path) -> "tuple[Optional[str], str, str]":
    ok, head = _git(wt, ["rev-parse", "HEAD"])
    if not ok or not head.strip():
        return None, REASON_GIT_UNMEASURABLE, f"HEAD: {head}"
    ok, status = _git(wt, ["status", "--porcelain"])
    if not ok:
        return None, REASON_GIT_UNMEASURABLE, f"status: {status}"
    if status.strip():
        return None, REASON_DIRTY, "uncommitted or untracked changes"
    return head.strip(), "", ""


def _liveness_refusal(dispatch_id: str, root: Path) -> "tuple[str, str]":
    if not dispatch_id:
        return "", ""
    from dispatch_process_registry import live_process_groups

    try:
        report = live_process_groups(dispatch_id, repo_root=root)
    except OSError as exc:
        return REASON_LIVENESS_UNKNOWN, f"registry not measurable: {exc}"
    if report.unmeasurable:
        return REASON_LIVENESS_UNKNOWN, report.unmeasurable
    if report.alive:
        return REASON_LIVENESS_ALIVE, f"process group(s) {list(report.alive)} still alive"
    if report.unknown:
        return REASON_LIVENESS_UNKNOWN, f"process group(s) {list(report.unknown)} not measurable"
    return "", ""


def _gate_seat(wt: Path, dry_run: bool) -> "tuple[Optional[int], Optional[GuardResult]]":
    sidecar = gate_lifetime_lock_path(wt)
    fd = None
    try:
        fd = os.open(sidecar, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, GuardResult(
            False, REASON_GATE_SEAT_LIVE, f"{sidecar} is held by a live gate seat", dry_run,
        )
    except FileNotFoundError:
        return None, GuardResult(False, REASON_GATE_SEAT_UNKNOWN, f"no sidecar at {sidecar}", dry_run)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        return None, GuardResult(False, REASON_GATE_SEAT_UNKNOWN, f"{sidecar}: {exc}", dry_run)
    return fd, None


def _dispatch_slot(
    dispatch_id: str, wt: Path, root: Path, dry_run: bool,
) -> "tuple[object | None, Optional[GuardResult]]":
    """Hold the dispatch's occupancy slot for the duration of the assessment.

    The slot is really acquired and released even when *dry_run* is true — the
    guard must prove that no process holds it — so a read-only run briefly
    holds a real slot, and a concurrent claim or reap for the same dispatch id
    can transiently observe it held.
    """
    import dispatch_worktree_isolation as dwi

    claims = dwi._claim_dir(root)
    reason = ""
    try:
        state = dwi.probe_occupancy(claims, dispatch_id)
        if state == "held":
            return None, GuardResult(False, REASON_OCCUPIED, f"dispatch {dispatch_id!r} holds the slot", dry_run)
        if state not in ("absent", "released"):
            return None, GuardResult(False, REASON_SLOT_UNAVAILABLE, f"occupancy state {state!r}", dry_run)
        fh, reason = dwi._hold_occupancy_for_reap(dispatch_id, wt, root, None)
        if fh is None and dwi.probe_occupancy(claims, dispatch_id) == "held":
            return None, GuardResult(False, REASON_OCCUPIED, reason, dry_run)
    except OSError as exc:
        return None, GuardResult(False, REASON_SLOT_UNAVAILABLE, f"occupancy not measurable: {exc}", dry_run)
    if fh is None:
        return None, GuardResult(False, REASON_SLOT_UNAVAILABLE, reason, dry_run)
    return fh, None


def _record_removal_decision(dispatch_id: str, wt: Path, head: str) -> str:
    """Persist the removal decision before the tree is removed (ADR-005).

    Returns ``""`` only when the line actually landed in the ledger; anything
    else is the named reason the write produced no recorded evidence.
    """
    try:
        from state_mutation import emit_state_mutation
    except ImportError as exc:
        return f"state_mutation is not importable: {exc}"
    # The existing helper carries three free fields; together they name the
    # dispatch, the tree and the evidence that cleared it.
    section = (
        f"head={head} dispatch_id={dispatch_id or '-'} "
        "evidence=clean status, HEAD unchanged under the repository lock"
    )
    try:
        result = emit_state_mutation(
            str(wt),
            trigger=f"worktree_remove:{dispatch_id or '-'}",
            section=section,
        )
    except Exception as exc:  # a failed ledger write must refuse, never remove
        return f"emit_state_mutation raised: {exc}"
    if result is None:
        return "emit_state_mutation reported no write"
    status = getattr(result, "status", None)
    if status not in ("appended", "duplicate"):
        return f"emit_state_mutation status {status!r} is not a recorded write"
    return ""


def _head_moved(expected_head: str, observed: str, where: str) -> "tuple[str, str]":
    return REASON_HEAD_MOVED, (
        f"HEAD moved {expected_head[:12]} -> {observed[:12]}{where}: "
        "the worktree no longer sits on the HEAD evaluate_candidate cleared"
    )


def _assess(
    wt: Path, root: Path, dispatch_id: str, expected_head: str, *, dry_run: bool,
    pause_hook: "Optional[Callable[[str], None]]",
    before_remove: "Optional[Callable[[str], None]]" = None,
) -> GuardResult:
    """Judge and (unless dry_run) remove, while the caller holds the slot.

    ``before_remove`` runs last among the steps that can refuse: under the
    repository lock, after the revalidation and after the removal decision is
    persisted, immediately before ``git worktree remove``. A result that keeps
    the tree therefore never had its lock dropped."""
    reason, detail = _liveness_refusal(dispatch_id, root)
    if not reason:
        head, reason, detail = _evidence(wt)
    if not reason and head != expected_head:
        reason, detail = _head_moved(expected_head, head, "")
    if not reason and pause_hook is not None:
        pause_hook("before_revalidation")
        again, reason, detail = _evidence(wt)
        if not reason and again != head:
            reason, detail = REASON_HEAD_MOVED, f"HEAD moved {head[:12]} -> {again[:12]}"
    if reason:
        return GuardResult(False, reason, detail, dry_run)
    if dry_run:
        return GuardResult(
            False, REASON_DRY_RUN, "clean under the slot", dry_run=True, would_remove=True,
        )
    with _worktree_lock(root):
        # Revalidate every call, not only with a pause_hook: taking the
        # repository lock can block, and anything written into the tree or
        # committed there during that wait must be seen before it is deleted.
        again, reason, detail = _evidence(wt)
        if not reason and again != expected_head:
            reason, detail = _head_moved(expected_head, again, " under the repository lock")
        if reason:
            return GuardResult(False, reason, detail, dry_run)
        failure = _record_removal_decision(dispatch_id, wt, again)
        if failure:
            return GuardResult(False, REASON_DECISION_UNRECORDED, failure, dry_run)
        # Every refusal is behind us now, so this is the first point at which
        # the caller may drop a lock: a tree the guard keeps keeps its lock.
        if before_remove is not None:
            before_remove("before_remove")
        ok, detail = _git(root, ["worktree", "remove", "--force", str(wt)])
    if ok:
        return GuardResult(True, REASON_REMOVED)
    return GuardResult(False, REASON_REMOVE_FAILED, detail)


def guarded_remove(
    worktree_path: Path,
    *,
    expected_head: str,
    dispatch_id: str,
    repo_root: Path,
    dry_run: bool = True,
    pause_hook: "Optional[Callable[[str], None]]" = None,
    before_remove: "Optional[Callable[[str], None]]" = None,
) -> GuardResult:
    """Remove only on evidence measured under the held slot; ``dry_run`` removes nothing.

    Every refusal has a reason.  The clean/HEAD evidence is taken again under
    the repository lock immediately before the removal, so a change made while
    the guard waited for that lock is refused instead of force-removed.
    *expected_head* is the sha ``evaluate_candidate`` judged: the removal is
    bound to it, and a tree whose HEAD is no longer that sha is refused with
    ``head_moved`` naming both shas — a commit made after the evidence
    evaluation never rides along on it.
    *pause_hook* (before the repository lock) exists so a test can move the
    evidence during the lock wait.  *before_remove* runs inside that lock
    after the revalidation and after the removal decision is persisted,
    immediately before ``git worktree remove`` — every refusal is behind it,
    so a hook that drops a lock can never unlock a tree the guard keeps.  The
    removal decision is persisted to the state-mutation ledger before
    ``git worktree remove``; if that write does not land, nothing is removed.
    """
    wt, root = Path(worktree_path), Path(repo_root)
    if not wt.exists():
        return GuardResult(False, REASON_ABSENT, f"{wt} does not exist", dry_run)
    if not expected_head:
        return GuardResult(
            False, REASON_HEAD_MOVED,
            "no evaluated HEAD sha to bind the removal to", dry_run,
        )
    gate = wt.name.startswith(_GATE_PREFIX) or gate_lifetime_lock_path(wt).exists()
    if gate:
        fd, refused = _gate_seat(wt, dry_run)
        if refused is not None:
            return refused
        try:
            return _assess(
                wt, root, dispatch_id, expected_head, dry_run=dry_run,
                pause_hook=pause_hook, before_remove=before_remove,
            )
        finally:
            os.close(fd)
            if not wt.exists():
                _release_lifetime_lock(wt)  # drops the sidecar once the tree is gone
    if not dispatch_id:
        return GuardResult(
            False, REASON_SLOT_UNAVAILABLE, "no dispatch_id for the occupancy slot", dry_run,
        )
    fh, refused = _dispatch_slot(dispatch_id, wt, root, dry_run)
    if refused is not None:
        return refused
    try:
        return _assess(
            wt, root, dispatch_id, expected_head, dry_run=dry_run,
            pause_hook=pause_hook, before_remove=before_remove,
        )
    finally:
        import dispatch_worktree_isolation as dwi

        dwi._unlock_and_close(fh, wt)
