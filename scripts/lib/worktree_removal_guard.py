"""worktree_removal_guard.py — the one gate before a worktree is removed (PR-4a).

Holds the tree's slot through the removal, keeps it when process liveness is
unknown, and revalidates HEAD + a clean status under the slot before removing.
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


def _assess(
    wt: Path, root: Path, dispatch_id: str, *, dry_run: bool,
    pause_hook: "Optional[Callable[[str], None]]",
) -> GuardResult:
    """Judge and (unless dry_run) remove, while the caller holds the slot."""
    reason, detail = _liveness_refusal(dispatch_id, root)
    if not reason:
        head, reason, detail = _evidence(wt)
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
        ok, detail = _git(root, ["worktree", "remove", "--force", str(wt)])
    if ok:
        return GuardResult(True, REASON_REMOVED)
    return GuardResult(False, REASON_REMOVE_FAILED, detail)


def guarded_remove(
    worktree_path: Path,
    *,
    dispatch_id: str,
    repo_root: Path,
    dry_run: bool = True,
    pause_hook: "Optional[Callable[[str], None]]" = None,
) -> GuardResult:
    """Remove only on evidence measured under the held slot; ``dry_run`` removes nothing.

    Every refusal has a reason; with a *pause_hook* the clean/HEAD evidence is
    taken once, the hook runs, and it is taken again, so a change in between is
    refused instead of force-removed.
    """
    wt, root = Path(worktree_path), Path(repo_root)
    if not wt.exists():
        return GuardResult(False, REASON_ABSENT, f"{wt} does not exist", dry_run)
    gate = wt.name.startswith(_GATE_PREFIX) or gate_lifetime_lock_path(wt).exists()
    if gate:
        fd, refused = _gate_seat(wt, dry_run)
        if refused is not None:
            return refused
        try:
            return _assess(wt, root, dispatch_id, dry_run=dry_run, pause_hook=pause_hook)
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
        return _assess(wt, root, dispatch_id, dry_run=dry_run, pause_hook=pause_hook)
    finally:
        import dispatch_worktree_isolation as dwi

        dwi._unlock_and_close(fh, wt)
