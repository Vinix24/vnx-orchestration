"""live_work.py — the "what is running right now" section of the T0 state.

Replaces ``active_work`` (a scan of ``dispatches/active/``, which the headless
door never fills), ``queues.active`` and the terminal lease view (the headless
lane uses no terminals).

Sources:
  * ``runtime_coordination.db`` table ``dispatches``, filtered on ``project_id``
    (ADR-007), rows in an in-flight state.
  * The occupancy flock ``<claims_dir>/<dispatch_id>.occupancy`` that
    ``dispatch_worktree_isolation._acquire_occupancy`` holds for the whole life
    of a dispatch. Liveness is "lock held", never "file exists" (thousands of old
    files stay behind) and never a lane deadline (a headless run may exceed 30
    minutes).
  * ``pr_queue.open_prs`` linked to their dispatch through the branch
    ``dispatch/<id>``.

The DB is opened read-only (``mode=ro``) with a bounded busy timeout. A failed
read yields ``{"available": False, "reason": "<exception>"}``, never an empty
list: an empty list reads as "nothing is running".
"""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordination_db import DB_FILENAME, DISPATCH_STATES, TERMINAL_DISPATCH_STATES

# Before execution: nothing runs yet. After a failed run: waiting on recovery.
_PRE_EXECUTION_STATES = frozenset({"proposed", "ready", "queued"})
_RECOVERY_STATES = frozenset({"timed_out", "failed_delivery", "recovered"})

# Derived from the state machine so a state added to DISPATCH_STATES joins the
# in-flight set by itself. Today: accepted, claimed, delivering, running.
IN_FLIGHT_STATES = (
    DISPATCH_STATES
    - TERMINAL_DISPATCH_STATES
    - _PRE_EXECUTION_STATES
    - _RECOVERY_STATES
)

# Time between the claim and the worktree (where the occupancy lock is taken).
STARTUP_GRACE_SECONDS = 120
DB_BUSY_TIMEOUT_SECONDS = 2

STATUS_LIVE = "live"
STATUS_STARTING = "starting"
STATUS_STALE = "stale"

BRANCH_PREFIX = "dispatch/"


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def occupancy_lock_held(claims_dir: Path, dispatch_id: str) -> bool:
    """True when a live process holds the occupancy flock for *dispatch_id*.

    Probes with a non-blocking shared lock: the holder takes LOCK_EX, so a
    conflict means it is still alive. The kernel drops the lock when a holder
    dies, so a crashed run reads as not held. A missing file is not held.
    """
    from dispatch_worktree_isolation import _sanitize_dispatch_id  # noqa: PLC0415

    lock_path = Path(claims_dir) / f"{_sanitize_dispatch_id(dispatch_id)}.occupancy"
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def dispatch_id_from_branch(branch: str) -> Optional[str]:
    """``dispatch/<id>`` -> ``<id>``; any other branch -> None."""
    if isinstance(branch, str) and branch.startswith(BRANCH_PREFIX):
        return branch[len(BRANCH_PREFIX):] or None
    return None


def _read_in_flight_rows(db_path: Path, project_id: str) -> List[Dict[str, Any]]:
    states = sorted(IN_FLIGHT_STATES)
    placeholders = ",".join("?" for _ in states)
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, timeout=DB_BUSY_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT_SECONDS * 1000)}")
        rows = conn.execute(
            "SELECT dispatch_id, state, terminal_id, track, gate, pr_ref, "
            "claimed_at, updated_at, created_at "
            "FROM dispatches WHERE project_id = ? "
            f"AND state IN ({placeholders}) ORDER BY created_at",
            (project_id, *states),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _link_open_prs(
    open_prs: List[Dict[str, Any]], known_ids: set
) -> List[Dict[str, Any]]:
    linked = []
    for pr in open_prs or []:
        branch = pr.get("branch") or ""
        linked.append({
            "number": pr.get("number"),
            "branch": branch,
            "ci_status": pr.get("ci_status"),
            "dispatch_id": dispatch_id_from_branch(branch),
            "dispatch_in_flight": dispatch_id_from_branch(branch) in known_ids,
        })
    return linked


def build_live_work(
    state_dir: Path,
    project_id: str,
    *,
    claims_dir: Optional[Path] = None,
    open_prs: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    startup_grace_seconds: int = STARTUP_GRACE_SECONDS,
) -> Dict[str, Any]:
    """Build the ``live_work`` section. Never raises."""
    pid = (project_id or "").strip()
    base: Dict[str, Any] = {
        "available": True,
        "reason": None,
        "project_id": pid,
        "in_flight_states": sorted(IN_FLIGHT_STATES),
        "startup_grace_seconds": startup_grace_seconds,
        "counts": {STATUS_LIVE: 0, STATUS_STARTING: 0, STATUS_STALE: 0},
        "dispatches": [],
        "open_prs": [],
    }
    if not pid:
        return {**base, "available": False, "reason": "project_id_unavailable"}

    state_dir = Path(state_dir)
    claims = Path(claims_dir) if claims_dir is not None else state_dir / "dispatch_worktree_claims"
    db_path = state_dir / DB_FILENAME
    if not db_path.exists():
        return {**base, "reason": "db_absent", "open_prs": _link_open_prs(open_prs or [], set())}

    try:
        rows = _read_in_flight_rows(db_path, pid)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {**base, "reason": "premigration", "open_prs": _link_open_prs(open_prs or [], set())}
        return {**base, "available": False, "reason": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        return {**base, "available": False, "reason": f"{type(exc).__name__}: {exc}"}

    current = now or datetime.now(timezone.utc)
    items: List[Dict[str, Any]] = []
    try:
        for row in rows:
            since = _parse_ts(row.get("updated_at")) or _parse_ts(row.get("claimed_at")) or _parse_ts(row.get("created_at"))
            age = int((current - since).total_seconds()) if since else None
            if occupancy_lock_held(claims, row["dispatch_id"]):
                status = STATUS_LIVE
            elif age is not None and age <= startup_grace_seconds:
                status = STATUS_STARTING
            else:
                status = STATUS_STALE
            base["counts"][status] += 1
            items.append({
                "dispatch_id": row["dispatch_id"],
                "state": row["state"],
                "status": status,
                "age_seconds": age,
                "track": row.get("track"),
                "gate": row.get("gate"),
                "terminal": row.get("terminal_id"),
                "pr_ref": row.get("pr_ref"),
            })
    except Exception as exc:
        return {**base, "available": False, "counts": {STATUS_LIVE: 0, STATUS_STARTING: 0, STATUS_STALE: 0},
                "reason": f"{type(exc).__name__}: {exc}"}

    base["dispatches"] = items
    base["open_prs"] = _link_open_prs(open_prs or [], {i["dispatch_id"] for i in items})
    return base


def live_work_index_summary(live_work: Optional[Dict[str, Any]], *, max_ids: int = 10) -> Dict[str, Any]:
    """Compact form for the always-loaded ``t0_index.json``."""
    lw = live_work or {}
    if not lw.get("available", False):
        return {"available": False, "reason": lw.get("reason") or "not_built"}
    items = lw.get("dispatches") or []

    def ids(status: str) -> List[str]:
        return [i["dispatch_id"] for i in items if i.get("status") == status][:max_ids]

    return {
        "available": True,
        "counts": dict(lw.get("counts") or {}),
        "live": ids(STATUS_LIVE),
        "starting": ids(STATUS_STARTING),
        "stale": ids(STATUS_STALE),
        "open_prs": [
            {"number": p.get("number"), "dispatch_id": p.get("dispatch_id")}
            for p in (lw.get("open_prs") or [])
        ][:max_ids],
    }
