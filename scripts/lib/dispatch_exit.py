"""ADR-045 (audit-spoor-kern K-5): one end line at every lane's exit.

K-3c built ``dispatch_lifecycle.finalize`` (the one writer of
``dispatch_finished``) and K-4a/K-4b/K-4c built the start line, the door's
hand-over and the spawn file. Nothing called the finalizer yet. This module is
the ONE call site shape every exit shares: it turns a lane's own outcome into
the frozen end line of ADR-045 §2/§8 (``end_state``, ``head_sha`` or an explicit
``no_push:<reason>``, ``spawned_at``, ``session_id``, ``reason``) and hands it
to the finalizer exactly once, BEFORE the lane releases its occupancy flock
(§5 — the lane must keep proving it is alive until after its line is published).

Two rules this module enforces for every caller:

* **A v3 hand-over only.** ``Handover.is_v3`` is fixed at the hand-over (§9), so
  a legacy dispatch — and every caller that passes no hand-over — takes exactly
  the K-4c path: no finalize, no line, no extra write.
* **A finalizer failure never rewrites the outcome.** A raising ``finalize`` is
  logged with the dispatch id and the exception and swallowed: the dispatch's
  own success stands (the plan's risk row: "K-5 boekt een geslaagde dispatch als
  failed"), and the lane still releases its flock.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dispatch_lifecycle import LifecycleResult, finalize
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, NO_PUSH_PREFIX

logger = logging.getLogger(__name__)

#: The closed reason vocabulary of an end line. ``completed`` carries none.
REASON_SLOT_TIMEOUT = "slot_timeout"
REASON_PRE_SPAWN_ERROR = "pre_spawn_error"
REASON_LANE_FAILED = "lane_failed"
REASON_TIMEOUT = "timeout"
REASON_DOOR_ERROR = "door_error"

#: ``no_push:<reason>`` (ADR-045 §8): an end line without a ``head_sha`` names
#: why there is none. Absence of both is not "no push" (receipt_schema refuses it).
NO_PUSH_NO_WORKTREE = "no_worktree"
NO_PUSH_HEAD_UNREADABLE = "head_unreadable"

#: The lane's own status vocabulary (``envelope_types._AdapterResult.status``).
_LANE_SUCCESS = "success"
_LANE_TIMEOUT = "timeout"

#: What a non-completed exit says when its caller named no sharper reason, so a
#: failed line is never written without one. ``lane_failed``, not ``worker_failed``:
#: the lane can fail around a worker that did not (OI-1743's close-out raise).
_DEFAULT_REASON = {
    "timed_out": REASON_TIMEOUT,
    "failed": REASON_LANE_FAILED,
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def end_state_for(status: Any, *, timed_out: bool = False) -> str:
    """The lane's status onto ADR-045 §2's closed set.

    Fail-closed: only an exact ``success`` is ``completed``; an absent, empty or
    unknown status is a failure, never a pass.
    """
    if str(status or "").strip() == _LANE_SUCCESS:
        return "completed"
    if timed_out or str(status or "").strip() == _LANE_TIMEOUT:
        return "timed_out"
    return "failed"


def worktree_head(wt_path: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(head_sha, None)`` or ``(None, why there is none)``. Never raises.

    Called at the exit while the worktree still exists — the teardown that
    releases the occupancy flock also deletes it (and with it this evidence).
    """
    if wt_path is None:
        return None, NO_PUSH_NO_WORKTREE
    try:
        probe = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, NO_PUSH_HEAD_UNREADABLE
    sha = probe.stdout.strip()
    if probe.returncode != 0 or not sha:
        return None, NO_PUSH_HEAD_UNREADABLE
    return sha, None


def build_end_line(
    handover: Any, *, end_state: str, reason: Optional[str] = None,
    head_sha: Optional[str] = None, no_push: Optional[str] = None,
    spawned_at: Optional[str] = None, session_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """The end line for this attempt, keyed on the hand-over (ADR-045 §2/§8).

    Optional fields are OMITTED when absent, never written as ``null``: the
    frozen intent is compared field by field, and ``spawned_at`` before a spawn
    must read as "there was none", not as an empty value.
    """
    line: Dict[str, Any] = {
        "timestamp": timestamp or now_utc(),
        "event_type": DISPATCH_FINISHED_EVENT_TYPE,
        "project_id": handover.project_id,
        "dispatch_id": handover.dispatch_id,
        "attempt": handover.attempt,
        "end_state": end_state,
    }
    for key, value in (("head_sha", head_sha), ("no_push", no_push),
                       ("spawned_at", spawned_at), ("session_id", session_id),
                       ("reason", reason)):
        if value:
            line[key] = value
    return line


def finalize_exit(
    handover: Any, *, state_dir: Any, status: Any, reason: Optional[str] = None,
    timed_out: bool = False, wt_path: Any = None, spawned_at: Optional[str] = None,
    session_id: Optional[str] = None, actor: str = "lifecycle",
) -> Optional[LifecycleResult]:
    """Publish this exit's one end line; ``None`` means "nothing to finalize".

    ``None`` is returned for a legacy hand-over (nothing may be written),
    for a hand-over without a usable attempt (OI-2510 p. 7: no key, no line),
    and for a finalizer that raised — which is logged, never raised, so it can
    neither turn a successful dispatch into a failed one nor keep the lane's
    occupancy flock from being released.
    """
    if handover is None or not getattr(handover, "is_v3", False):
        return None
    attempt = getattr(handover, "attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    end_state = end_state_for(status, timed_out=timed_out)
    reason = reason or _DEFAULT_REASON.get(end_state)
    head_sha, absent = worktree_head(wt_path)
    line = build_end_line(
        handover, end_state=end_state, reason=reason, head_sha=head_sha,
        no_push=None if head_sha else f"{NO_PUSH_PREFIX}{absent}",
        spawned_at=spawned_at, session_id=session_id,
    )
    try:
        return finalize(
            state_dir=state_dir, project_id=handover.project_id,
            dispatch_id=handover.dispatch_id, attempt=attempt,
            end_state=end_state, end_line=line, actor=actor, reason=reason,
        )
    except Exception as exc:  # vnx-silent-except: a finalizer failure must not fail the dispatch it describes
        logger.warning(
            "[dispatch_exit] finalize failed dispatch_id=%s attempt=%s end_state=%s: %r",
            handover.dispatch_id, attempt, end_state, exc,
        )
        return None


def finalize_lane_exit(
    handover: Any, *, state_dir: Any, result: Any, wt_path: Any = None,
    spawned_at: Optional[str] = None, reason: Optional[str] = None,
) -> Optional[LifecycleResult]:
    """A lane's exit: the adapter result is the outcome, its session id the link."""
    return finalize_exit(
        handover, state_dir=state_dir, status=getattr(result, "status", None),
        timed_out=bool(getattr(result, "timed_out", False)), reason=reason,
        wt_path=wt_path, spawned_at=spawned_at,
        session_id=getattr(result, "session_id", None),
    )
