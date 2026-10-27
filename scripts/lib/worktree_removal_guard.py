"""worktree_removal_guard.py — the one guard before a worktree is removed (PR-4a).

RED-RUN STUB. The guard itself lands in the next commit; this file exists so
the acceptance tests import a real signature instead of failing on ImportError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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


@dataclass(frozen=True)
class GuardResult:
    removed: bool
    reason: str
    detail: str = ""
    dry_run: bool = False
    would_remove: bool = False

    @property
    def refused(self) -> bool:
        return not self.removed and not self.would_remove


def guarded_remove(
    worktree_path: Path,
    *,
    dispatch_id: str,
    repo_root: Path,
    dry_run: bool = True,
    data_dir: Optional[Path] = None,
    pause_hook: "Optional[Callable[[str], None]]" = None,
) -> GuardResult:
    """RED-RUN STUB: the real guard lands in the next commit."""
    raise NotImplementedError("guarded_remove lands in the next commit (PR-4a)")
