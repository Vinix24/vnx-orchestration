"""ADR-045 (audit-spoor-kern K-5): one end line at every lane's exit.

K-3c built ``dispatch_lifecycle.finalize`` (the one writer of
``dispatch_finished``) and K-4a/K-4b/K-4c built the start line, the door's
hand-over and the spawn file. Nothing called the finalizer yet. This module is
the ONE call site shape every exit shares: it turns a lane's own outcome into
the frozen end line of ADR-045 §2/§8 (``end_state``, ``head_sha`` or an explicit
``no_push:<reason>``, ``spawned_at``, ``session_id``, ``reason``) and hands it
to the finalizer exactly once, BEFORE the lane releases its occupancy flock
(§5 — the lane must keep proving it is alive until after its line is published).

The end line follows the GOVERNED status (K-5 round 2): ``GOVERNED_END_STATES`` is
the one table from what governance decided onto the end state and its reason — a
worker ``success`` that governance could not prove is ``failed``, never ``completed``.

Two rules for every caller. **A v3 hand-over only**: ``Handover.is_v3`` is fixed at
the hand-over (§9), so a legacy dispatch — and every caller that passes no
hand-over — takes exactly the K-4c path: no finalize, no line, no extra write.
**A finalizer failure never rewrites the outcome**: a raising ``finalize`` is logged
with the dispatch id and swallowed, so the dispatch's own success stands and the
lane still releases its flock.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dispatch_lifecycle import LifecycleResult, finalize
from envelope_types import PHANTOM_REJECTED_STATUS
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, NO_PUSH_PREFIX
# OI-2519: the one literal GOVERN stamps for a report that fails the body
# contract — the end-line table below keys on it, never on a second literal.
from report_body_contract import CONTRACT_INVALID_STATUS

logger = logging.getLogger(__name__)

#: The closed reason vocabulary of an end line. ``completed`` carries none.
REASON_SLOT_TIMEOUT = "slot_timeout"
REASON_PRE_SPAWN_ERROR = "pre_spawn_error"
REASON_LANE_FAILED = "lane_failed"
REASON_TIMEOUT = "timeout"
REASON_DOOR_ERROR = "door_error"
#: A ``_govern`` that raised: no report, no receipt — never the worker's outcome.
REASON_GOVERN_ERROR = "govern_error"

#: ``no_push:<reason>`` (ADR-045 §8): an end line without a ``head_sha`` names
#: why there is none. Absence of both is not "no push" (receipt_schema refuses it).
NO_PUSH_NO_WORKTREE = "no_worktree"
NO_PUSH_HEAD_UNREADABLE = "head_unreadable"

#: The lane's own status vocabulary (``envelope_types._AdapterResult.status``).
_LANE_SUCCESS = "success"
_LANE_TIMEOUT = "timeout"

#: ADR-045 §2/§5 — THE table: a lane's GOVERNED status (what ``_envelope_outcome``
#: decided AFTER GOVERN ran, never the worker's raw status) onto ``(end_state, the
#: reason the line carries when the caller names none)``. ``lane_failed``, not
#: ``worker_failed``: the lane can fail around a worker that did not (OI-1743).
GOVERNED_END_STATES: Dict[str, Tuple[str, Optional[str]]] = {
    _LANE_SUCCESS: ("completed", None),
    "failure": ("failed", REASON_LANE_FAILED),
    _LANE_TIMEOUT: ("timed_out", REASON_TIMEOUT),
    # OI-1287: the work succeeded but GOVERN produced no receipt — never a pass.
    "receipt_missing": ("failed", "receipt_missing"),
    # OI-2519: the work succeeded but GOVERN rejected the report body — a
    # governed refusal is never `completed`, and the reason names it.
    CONTRACT_INVALID_STATUS: ("failed", CONTRACT_INVALID_STATUS),
    # OI-2519 (K-5b r2): the work claimed success but the phantom guard found no
    # deliverable — the delivery was rejected, so the attempt is never a pass.
    PHANTOM_REJECTED_STATUS: ("failed", PHANTOM_REJECTED_STATUS),
}

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def end_state_for(status: Any, *, timed_out: bool = False) -> Tuple[str, Optional[str]]:
    """``(end_state, reason)`` for a lane's GOVERNED status, from the one table.

    Fail-closed: only an exact ``success`` is ``completed`` and only a timeout (the
    status or its own flag) is ``timed_out``; everything else is ``failed`` with a
    reason that names the cause.
    """
    key = str(status or "").strip()
    if timed_out and key != _LANE_SUCCESS:
        key = _LANE_TIMEOUT
    return GOVERNED_END_STATES.get(key, ("failed", REASON_LANE_FAILED))


def worktree_head(wt_path: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(head_sha, None)`` or ``(None, why there is none)``. Never raises.

    Called at the exit while the worktree still exists: the teardown that releases
    the occupancy flock deletes it, and this evidence with it.
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
    handover: Any, *, role: str, end_state: str, reason: Optional[str] = None,
    head_sha: Optional[str] = None, no_push: Optional[str] = None,
    spawned_at: Optional[str] = None, session_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """The end line for this attempt, keyed on the hand-over (ADR-045 §2/§8).

    ``role`` is keyword-only and has no default: the end line carries the same
    identity fields as the start line, so a writer that forgets it fails at once
    instead of publishing a line that cannot be joined to its start.

    Optional fields are OMITTED when absent, never written as ``null``: the frozen
    intent is compared field by field, and a pre-spawn ``spawned_at`` must read as
    "there was none", not as an empty value.
    """
    line: Dict[str, Any] = {
        "timestamp": timestamp or now_utc(),
        "event_type": DISPATCH_FINISHED_EVENT_TYPE,
        "project_id": handover.project_id,
        "dispatch_id": handover.dispatch_id,
        "attempt": handover.attempt,
        "role": role,
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

    ``None`` means nothing may be written: a legacy hand-over, a hand-over without
    a usable attempt (OI-2510 p. 7: no key, no line), or a finalizer that raised —
    which is logged, never raised, so it can neither fail a successful dispatch nor
    keep the lane's occupancy flock from being released.
    """
    if handover is None or not getattr(handover, "is_v3", False):
        return None
    attempt = getattr(handover, "attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return None
    end_state, default_reason = end_state_for(status, timed_out=timed_out)
    reason = reason or default_reason
    head_sha, absent = worktree_head(wt_path)
    line = build_end_line(
        handover, role=handover.role, end_state=end_state, reason=reason, head_sha=head_sha,
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
    handover: Any, *, state_dir: Any, status: Any, result: Any, wt_path: Any = None,
    spawned_at: Optional[str] = None, reason: Optional[str] = None,
) -> Optional[LifecycleResult]:
    """A lane's exit: the GOVERNED status is the outcome, the worker's session id the link.

    *status* is required: what ``_envelope_outcome`` decided after GOVERN ran,
    never ``result.status`` itself, so an unproven worker success is never booked
    ``completed`` (ADR-045 §2/§5). *result* supplies the session id and timer.
    """
    return finalize_exit(
        handover, state_dir=state_dir, status=status,
        timed_out=bool(getattr(result, "timed_out", False)), reason=reason,
        wt_path=wt_path, spawned_at=spawned_at,
        session_id=getattr(result, "session_id", None),
    )
