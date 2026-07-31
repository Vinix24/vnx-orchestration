"""dispatch_process_registry.py — record & reap dispatch-scoped process groups (OI-877).

A dispatch worker's session hooks can leave processes behind that outlive the
dispatch and run entirely OUTSIDE the dispatch worktree.  Their repo-root
resolves to the main checkout (the git common dir), so the worktree-scoped
lsof scan in ``worktree_process_cleanup.kill_worktree_processes`` cannot see
them — measured 2026-07-31: a ``session_reconcile_autoclose.sh``-spawned
``planning_cli.py objective reconcile`` survived dispatch teardown and held the
coordination DB write lock, blocking ``link_sessions_dispatches.py``.

This module records the worker's process group(s) at spawn time — the one
moment the lane still knows the group — and re-finds them at teardown by PGID.
A process that has run away from the group (reparented to launchd, PPID 1)
keeps its PGID, so it is still found.

Conservatism: the registry only ever contains what a dispatch lane explicitly
recorded, and teardown only signals a group when at least one recorded member
PID still exists AND still carries the recorded PGID.  If there is any doubt
(no entry, entry cleared, every member gone, or the PGID reused by an
unrelated group), the kill does nothing and logs instead of guessing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Matches the sanitization in dispatch_worktree_isolation so the registry key
# for a dispatch is identical whichever teardown path resolves it.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SAFE_ID_LEN = 60

# SIGTERM grace period before SIGKILL — mirrors worktree_process_cleanup.
_GRACE_SECONDS = 0.3


def _sanitize_dispatch_id(dispatch_id: str) -> str:
    """Return a filesystem-safe key for *dispatch_id*."""
    return _UNSAFE_RE.sub("-", dispatch_id)[:_MAX_SAFE_ID_LEN]


def _registry_dir(repo_root: Path) -> Path:
    """Return the per-repo directory holding one JSON entry per dispatch."""
    return Path(repo_root).resolve() / ".vnx-data" / "state" / "dispatch_pgids"


def _entry_path(repo_root: Path, dispatch_id: str) -> Path:
    return _registry_dir(repo_root) / f"{_sanitize_dispatch_id(dispatch_id)}.json"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _snapshot_group_members(pgid: int) -> list[int]:
    """Return the member PIDs of *pgid* right now (empty on any failure)."""
    try:
        result = subprocess.run(
            ["ps", "-g", str(pgid), "-o", "pid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [
            int(line.strip())
            for line in result.stdout.splitlines()
            if line.strip().isdigit()
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("dispatch_process_registry: ps -g %s failed: %s", pgid, exc)
        return []


def _load_entry(
    dispatch_id: str, *, repo_root: Path
) -> "dict[str, object] | None":
    """Read the registry entry for *dispatch_id*; None when absent or corrupt."""
    path = _entry_path(repo_root, dispatch_id)
    try:
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)
        if not isinstance(entry, dict):
            return None
        return entry
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "dispatch_process_registry: unreadable entry %s: %s — "
            "treating as absent (nothing to kill)",
            path,
            exc,
        )
        return None


def _write_entry(dispatch_id: str, *, repo_root: Path, entry: dict) -> None:
    """Atomically persist the registry entry for *dispatch_id*."""
    from atomic_io import atomic_write_text  # noqa: PLC0415

    path = _entry_path(repo_root, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(entry, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_dispatch_pgids(
    dispatch_id: str,
    pgids: Iterable[int],
    *,
    repo_root: Path,
) -> None:
    """Record process groups belonging to *dispatch_id* for later teardown.

    Each group is snapshotted with its current member PIDs so teardown can
    verify the group is still the one this dispatch spawned (PID/PGID reuse
    guard).  Appends to any existing entry for the same dispatch.  Never
    raises: a failed record degrades to "worktree scan only" teardown.
    """
    if not dispatch_id or not pgids:
        return
    entry = _load_entry(dispatch_id, repo_root=repo_root) or {}
    groups: dict[str, list[int]] = dict(entry.get("groups") or {})
    for pgid in pgids:
        try:
            pgid_int = int(pgid)
        except (TypeError, ValueError):
            continue
        members = _snapshot_group_members(pgid_int)
        if members:
            groups[str(pgid_int)] = members
    if not groups:
        return
    entry["dispatch_id"] = dispatch_id
    entry["recorded_at"] = time.time()
    entry["groups"] = groups
    try:
        _write_entry(dispatch_id, repo_root=repo_root, entry=entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispatch_process_registry: failed to record pgids for %s: %s — "
            "teardown will fall back to the worktree scan only",
            dispatch_id,
            exc,
        )


def load_dispatch_pgids(dispatch_id: str, *, repo_root: Path) -> list[int]:
    """Return the recorded PGIDs for *dispatch_id* (empty when none)."""
    entry = _load_entry(dispatch_id, repo_root=repo_root)
    if not entry:
        return []
    groups = entry.get("groups") or {}
    result: list[int] = []
    for pgid_str in groups:
        try:
            result.append(int(pgid_str))
        except (TypeError, ValueError):
            continue
    return sorted(result)


def clear_dispatch_pgids(dispatch_id: str, *, repo_root: Path) -> None:
    """Remove the registry entry for *dispatch_id*.  Idempotent, never raises."""
    try:
        _entry_path(repo_root, dispatch_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(
            "dispatch_process_registry: clear failed for %s: %s",
            dispatch_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def _group_is_valid(pgid: int, member_pids: list[int]) -> bool:
    """True when a recorded member still exists and still carries *pgid*.

    Guards against PGID reuse: a group whose PGID was handed to an unrelated
    process will not have any recorded member PID in it.
    """
    for pid in member_pids:
        try:
            if os.getpgid(pid) == pgid:
                return True
        except (ProcessLookupError, PermissionError):
            continue
    return False


def kill_dispatch_pgids(
    dispatch_id: str,
    *,
    repo_root: Path,
    grace: float = _GRACE_SECONDS,
) -> int:
    """Kill the recorded process groups of *dispatch_id*.

    SIGTERM then, after *grace*, SIGKILL — mirroring
    ``worktree_process_cleanup.kill_worktree_processes``.  The caller's own
    process group is never signalled.  A group is only signalled when at least
    one recorded member PID is still alive in it.

    Returns the number of process groups signalled (0 when nothing was
    provably this dispatch's).
    """
    entry = _load_entry(dispatch_id, repo_root=repo_root)
    if not entry:
        logger.debug(
            "dispatch_process_registry: no pgid entry for %s — nothing to do",
            dispatch_id,
        )
        return 0
    groups = entry.get("groups") or {}

    own_pgid = os.getpgid(0)
    targets: list[int] = []
    for pgid_str, member_pids in groups.items():
        try:
            pgid = int(pgid_str)
        except (TypeError, ValueError):
            continue
        if pgid == own_pgid:
            logger.debug(
                "dispatch_process_registry: skip pgid %s for %s (own group)",
                pgid,
                dispatch_id,
            )
            continue
        members = (
            [int(p) for p in member_pids if str(p).isdigit()]
            if isinstance(member_pids, list)
            else []
        )
        if _group_is_valid(pgid, members):
            targets.append(pgid)
        else:
            logger.warning(
                "dispatch_process_registry: NOT killing pgid %s for %s — "
                "no recorded member alive with that PGID (group gone or PGID "
                "reused); leaving it untouched",
                pgid,
                dispatch_id,
            )

    if not targets:
        logger.debug(
            "dispatch_process_registry: no valid target groups for %s",
            dispatch_id,
        )
        return 0

    for pgid in targets:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(grace)
    for pgid in targets:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    logger.info(
        "dispatch_process_registry: signalled %d process group(s) for dispatch %s",
        len(targets),
        dispatch_id,
    )
    return len(targets)


def cleanup_dispatch_processes(
    dispatch_id: str,
    wt_path: Path,
    *,
    repo_root: Path,
) -> int:
    """Kill dispatch processes via BOTH teardown mechanisms, then clear.

    Mechanism 1 (existing): the worktree-scoped lsof scan — finds processes
    with files or CWD inside *wt_path*.  Mechanism 2 (this module): the
    pgid registry — finds dispatch processes recorded at spawn that run
    outside the worktree.

    Returns the total number of process groups signalled.
    """
    total = 0
    try:
        from worktree_process_cleanup import kill_worktree_processes  # noqa: PLC0415

        total += kill_worktree_processes(wt_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispatch_process_registry: worktree scan failed for %s: %s",
            dispatch_id,
            exc,
        )
    total += kill_dispatch_pgids(dispatch_id, repo_root=repo_root)
    clear_dispatch_pgids(dispatch_id, repo_root=repo_root)
    return total


# ---------------------------------------------------------------------------
# Spawn-time capture helper (tmux lane)
# ---------------------------------------------------------------------------

def collect_descendant_pgids(root_pid: int) -> set[int]:
    """Return the PGIDs of *root_pid* and every descendant of it.

    Used by the tmux lane to record the worker's process groups right after
    the SessionStart hooks have fired — at that point any hook-spawned
    background process is already a member of the captured groups, so the
    escaped orphan is re-findable at teardown even after reparenting.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    except Exception as exc:  # noqa: BLE001
        logger.debug("dispatch_process_registry: ps -axo failed: %s", exc)
        return set()

    children: dict[int, list[int]] = {}
    pgid_of: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        pgid_of[pid] = pgid
        children.setdefault(ppid, []).append(pid)

    seen: set[int] = set()
    stack = [root_pid]
    pgids: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in pgid_of:
            pgids.add(pgid_of[pid])
        stack.extend(children.get(pid, []))
    return pgids
