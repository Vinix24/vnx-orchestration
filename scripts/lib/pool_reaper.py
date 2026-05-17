"""pool_reaper.py — Identify stuck/stale workers, prepare reap actions.

Pure detection module. Actual SIGTERM/SIGKILL via cleanup_worker_exit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from pool_decision_engine import Membership


@dataclass(frozen=True)
class ReapTarget:
    membership_id: str
    terminal_id: str
    pid: Optional[int]
    reason: str  # e.g. "heartbeat_stale=200s>180s", "never_heartbeat_age=200s>180s"


@dataclass(frozen=True)
class ReapConfig:
    heartbeat_stale_threshold_s: float = 180.0  # operator-tunable; reap after this long silent
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
