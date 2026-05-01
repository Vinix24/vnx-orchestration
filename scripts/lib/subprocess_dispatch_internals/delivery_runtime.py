"""delivery_runtime — _SubprocessResult, heartbeat thread loop."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


class _SubprocessResult(NamedTuple):
    """Return value from deliver_via_subprocess() carrying stats back to the caller."""
    success: bool
    session_id: str | None
    event_count: int
    manifest_path: str | None
    # Repo-relative paths the worker explicitly wrote/edited via structured tool
    # calls (Write/Edit/MultiEdit/NotebookEdit) during this dispatch.  Used by
    # _auto_commit_changes / _auto_stash_changes to scope staging to *this*
    # worker's writes, even in shared worktrees where concurrent terminals or
    # the operator may produce additional dirty files during the dispatch
    # window.  Empty frozenset() when no structured file writes occurred.
    touched_files: frozenset[str] = frozenset()


def _heartbeat_loop(
    terminal_id: str,
    dispatch_id: str,
    generation: int,
    stop_event: threading.Event,
    state_dir: Path,
    interval: float = 300.0,
) -> None:
    """Renew lease every *interval* seconds until stop_event is set."""
    while not stop_event.wait(timeout=interval):
        try:
            from lease_manager import LeaseManager
            lm = LeaseManager(state_dir=state_dir, auto_init=False)
            lm.renew(terminal_id, generation=generation, actor="heartbeat")
            logger.info("Heartbeat renewed lease for %s (gen %d)", terminal_id, generation)
        except Exception as e:
            logger.warning("Heartbeat renewal failed for %s: %s", terminal_id, e)
