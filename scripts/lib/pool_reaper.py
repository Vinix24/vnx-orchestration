"""pool_reaper.py — Identify stuck/stale workers, prepare reap actions.

Pure detection module. Actual SIGTERM/SIGKILL via cleanup_worker_exit.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, List, Optional

from pool_decision_engine import POOL_HEARTBEAT_STALE_SECONDS

if TYPE_CHECKING:
    from pool_decision_engine import Membership

log = logging.getLogger(__name__)

_VNX_SESSION_PREFIX = "vnx-"


def _sanitize_session_name(raw: str) -> str:
    """tmux session names may not contain '.' or ':'. Map them to '-'."""
    return "".join("-" if c in ".:" else c for c in raw)


def list_tmux_sessions() -> list[str]:
    """Return a list of tmux session names currently running."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 - fail-soft, tmux may not be available
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def session_has_living_children(session_name: str) -> bool:
    """Return True when *session_name* has at least one living child process.

    Reads pane PIDs from tmux and probes each with os.kill(pid, 0).
    An empty result (no panes, or tmux not running) is treated as no children.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 - fail-soft, tmux may not be available
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            pid = int(stripped)
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            continue
    return False


def identify_orphan_tmux_sessions(
    sessions: list[str],
    has_children_fn: Callable[[str], bool],
    is_terminal_fn: Callable[[str], bool],
) -> list[tuple[str, str]]:
    """Identify orphan tmux sessions matching ``vnx-*``.

    A session is an orphan when all of these hold:
    - Name starts with ``vnx-``
    - No living child processes (checked via *has_children_fn*)
    - The dispatch (derived from the session name by stripping the prefix)
      is in a terminal state (checked via *is_terminal_fn*)

    Returns a list of ``(session_name, reason)`` tuples ready for teardown.
    """
    orphans: list[tuple[str, str]] = []
    for session in sessions:
        if not session.startswith(_VNX_SESSION_PREFIX):
            continue
        if has_children_fn(session):
            continue
        dispatch_id = session[len(_VNX_SESSION_PREFIX):]
        if not dispatch_id:
            continue
        if is_terminal_fn(dispatch_id):
            orphans.append((session, "orphan_tmux_session: no children, dispatch terminal"))
    return orphans


def sweep_orphan_tmux_sessions(
    is_terminal_fn: Callable[[str], bool],
) -> list[str]:
    """List, identify, and kill orphan tmux sessions. Returns killed session names.

    Fail-soft: a session that no longer exists or a failed kill does not abort
    the sweep. Each killed session is logged with name, age, and reason.
    """
    sessions = list_tmux_sessions()
    orphans = identify_orphan_tmux_sessions(
        sessions,
        has_children_fn=session_has_living_children,
        is_terminal_fn=is_terminal_fn,
    )
    killed: list[str] = []
    for session_name, reason in orphans:
        try:
            result = subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                killed.append(session_name)
                log.info(
                    "orphan_tmux_sweep: killed session=%s reason=%s",
                    session_name, reason,
                )
            else:
                log.warning(
                    "orphan_tmux_sweep: kill-session failed for %s (rc=%d): %s",
                    session_name, result.returncode,
                    (result.stderr or "").strip(),
                )
        except Exception as exc:
            log.warning(
                "orphan_tmux_sweep: kill-session error for %s: %s",
                session_name, exc,
            )
    if killed:
        log.info("orphan_tmux_sweep: %d session(s) killed", len(killed))
    return killed


@dataclass(frozen=True)
class ReapTarget:
    membership_id: str
    terminal_id: str
    pid: Optional[int]
    reason: str  # e.g. "heartbeat_stale=200s>180s", "never_heartbeat_age=200s>180s"


@dataclass(frozen=True)
class ReapConfig:
    heartbeat_stale_threshold_s: float = POOL_HEARTBEAT_STALE_SECONDS
    stuck_threshold_s: float = 300.0            # reserved for future processing-stuck detection
    warmup_window_s: float = 120.0              # workers younger than this are exempt


def identify_reap_targets(
    members: "List[Membership]",
    now: float,
    config: ReapConfig,
) -> List[ReapTarget]:
    """Pure function: which active members are reap-eligible?

    Eligibility rules applied in order:
    - status != 'active'  → skip (already draining/reaped)
    - worker_age < warmup_window_s → skip (respect startup warmup)
    - last_heartbeat is None AND worker_age > heartbeat_stale_threshold_s → reap
    - last_heartbeat is not None AND (now - last_heartbeat) > heartbeat_stale_threshold_s → reap
    """
    targets: List[ReapTarget] = []
    for m in members:
        if m.status != "active":
            continue

        worker_age = now - m.joined_at
        if worker_age < config.warmup_window_s:
            continue

        if m.last_heartbeat is None:
            if worker_age > config.heartbeat_stale_threshold_s:
                targets.append(ReapTarget(
                    membership_id=m.membership_id,
                    terminal_id=m.terminal_id,
                    pid=m.pid,
                    reason=(
                        f"never_heartbeat_age={worker_age:.0f}s"
                        f">{config.heartbeat_stale_threshold_s:.0f}s"
                    ),
                ))
        else:
            stale_age = now - m.last_heartbeat
            if stale_age > config.heartbeat_stale_threshold_s:
                targets.append(ReapTarget(
                    membership_id=m.membership_id,
                    terminal_id=m.terminal_id,
                    pid=m.pid,
                    reason=(
                        f"heartbeat_stale={stale_age:.0f}s"
                        f">{config.heartbeat_stale_threshold_s:.0f}s"
                    ),
                ))
    return targets


def is_pid_alive(pid: Optional[int]) -> bool:
    """Check if a process with the given PID is still running.

    Returns False for None, pid <= 0, or dead processes.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def identify_dead_pid_targets(
    members: "List[Membership]",
) -> List[ReapTarget]:
    """Identify active members whose worker PID is no longer alive."""
    targets: List[ReapTarget] = []
    for m in members:
        if m.status != "active":
            continue
        if m.pid is None or m.pid <= 0:
            continue
        if not is_pid_alive(m.pid):
            targets.append(ReapTarget(
                membership_id=m.membership_id,
                terminal_id=m.terminal_id,
                pid=m.pid,
                reason=f"pid_dead={m.pid}",
            ))
    return targets
