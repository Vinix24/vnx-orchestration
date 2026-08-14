"""delivery_runtime — _SubprocessResult, heartbeat thread loops."""

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


def _silence_heartbeat_loop(
    terminal_id: str,
    dispatch_id: str,
    stop_event: threading.Event,
    *,
    interval: float = 30.0,
    process_cell: list | None = None,
    adapter: object | None = None,
    model: str = "",
) -> None:
    """Monitor the EventStore for silence and kill the worker if stuck.

    Checks EventStore.last_event(terminal_id) every *interval* seconds.
    If the last event is older than the configured silence threshold,
    the worker is killed via *adapter*.kill(terminal_id) (when available)
    and a failure report is written.

    *process_cell* is a mutable list like [process_result] that holds the
    adapter reference once spawn_claude() has started the subprocess.
    It is updated by the caller after spawn.

    When neither adapter nor process_cell is provided, silence is only
    logged — the heartbeat cannot kill without a process reference.
    """
    while not stop_event.wait(timeout=interval):
        try:
            from worker_heartbeat import (  # noqa: PLC0415
                EventStreamHeartbeat,
                SilenceVerdict,
                build_heartbeat_failure_report,
            )
            from event_store import EventStore  # noqa: PLC0415
        except ImportError as exc:
            logger.debug(
                "delivery_runtime: silence heartbeat init failed for %s: %s",
                terminal_id, exc,
            )
            continue

        try:
            store = EventStore()
            hb = EventStreamHeartbeat(
                terminal_id, dispatch_id,
                events_dir=store._events_dir,
            )
            verdict = hb.check()

            if not verdict.is_silent:
                continue

            # Worker is silent — log and attempt to kill.
            logger.warning(
                "delivery_runtime: silence heartbeat triggered for %s "
                "(%.0fs silent, threshold=%.0fs) — attempting kill",
                dispatch_id,
                verdict.silence_seconds,
                verdict.threshold_seconds,
            )

            # Resolve the adapter — prefer the one passed directly, then
            # check process_cell for a spawn result that carries _adapter.
            effective_adapter = adapter
            if effective_adapter is None and process_cell is not None and len(process_cell) > 0:
                spawn_result = process_cell[0]
                if spawn_result is not None:
                    effective_adapter = getattr(spawn_result, "_adapter", None)

            if effective_adapter is not None:
                try:
                    # SubprocessAdapter.stop() does SIGTERM → SIGKILL on timeout.
                    effective_adapter.stop(terminal_id)
                    logger.warning(
                        "delivery_runtime: killed worker process for %s via adapter.stop()",
                        terminal_id,
                    )
                except Exception as kill_exc:
                    logger.debug(
                        "delivery_runtime: adapter.stop() failed for %s: %s",
                        terminal_id, kill_exc,
                    )

            # Write a failure report.
            try:
                _report = build_heartbeat_failure_report(
                    dispatch_id=dispatch_id,
                    verdict=verdict,
                    model=model,
                    terminal_id=terminal_id,
                )
                from vnx_paths import resolve_paths
                _data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
                _reports_dir = _data_dir / "unified_reports"
                _reports_dir.mkdir(parents=True, exist_ok=True)
                _report_path = _reports_dir / f"{dispatch_id}.md"
                _report_path.write_text(_report, encoding="utf-8")
                logger.info(
                    "delivery_runtime: silence heartbeat failure report "
                    "written to %s", _report_path,
                )
            except Exception as report_exc:
                logger.debug(
                    "delivery_runtime: silence heartbeat report "
                    "write failed for %s: %s",
                    dispatch_id, report_exc,
                )

            # One kill attempt is enough — stop the loop.
            stop_event.set()
            return

        except Exception as exc:
            logger.debug(
                "delivery_runtime: silence heartbeat check failed "
                "for %s: %s", terminal_id, exc,
            )
