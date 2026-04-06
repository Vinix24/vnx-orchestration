#!/usr/bin/env python3
"""VNX Subprocess Health Monitor — Polls active subprocess workers and classifies health.

Periodically checks all registered subprocess workers via SubprocessAdapter.health(),
classifies their state, and routes failures through WorkflowSupervisor.handle_incident().

Worker states:
  - healthy:  process alive and producing events recently
  - stalled:  process alive but no events for >120s
  - dead:     process exited (poll() returned exit code)
  - hung:     process alive but no output for >300s

BILLING SAFETY: No Anthropic SDK. Uses stdlib (threading, json, time) + existing VNX infra.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from incident_taxonomy import IncidentClass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = 30.0       # seconds between health checks
STALL_THRESHOLD = 120.0    # no events for this long → stalled
HUNG_THRESHOLD = 300.0     # alive but no output for this long → hung

# ---------------------------------------------------------------------------
# Worker classification
# ---------------------------------------------------------------------------


@dataclass
class WorkerInfo:
    """Tracked state for a single subprocess worker."""
    terminal_id: str
    dispatch_id: str
    registered_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    pid: Optional[int] = None


class WorkerClassification:
    HEALTHY = "healthy"
    STALLED = "stalled"
    DEAD = "dead"
    HUNG = "hung"


def classify_worker(
    health_result,
    worker: WorkerInfo,
    now: float,
) -> str:
    """Classify a worker's state based on health check and event timing.

    Args:
        health_result: HealthResult from SubprocessAdapter.health()
        worker: Tracked WorkerInfo for this terminal
        now: Current time (time.time())

    Returns one of: healthy, stalled, dead, hung
    """
    if not health_result.process_alive:
        return WorkerClassification.DEAD

    elapsed_since_event = now - worker.last_event_at

    if elapsed_since_event > HUNG_THRESHOLD:
        return WorkerClassification.HUNG

    if elapsed_since_event > STALL_THRESHOLD:
        return WorkerClassification.STALLED

    return WorkerClassification.HEALTHY


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

def _default_state_dir() -> Path:
    """Resolve VNX state directory from environment."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return Path(__file__).resolve().parent.parent / ".vnx-data" / "state"


def _default_log_dir() -> Path:
    """Resolve VNX log directory from environment."""
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "logs"
    return Path(__file__).resolve().parent.parent / ".vnx-data" / "logs"


class SubprocessHealthMonitor:
    """Monitors subprocess worker health and routes failures to WorkflowSupervisor.

    Usage:
        monitor = SubprocessHealthMonitor(adapter)
        monitor.register("T1", "dispatch-123")
        monitor.start()  # runs polling in background thread
        ...
        monitor.stop()
    """

    def __init__(
        self,
        adapter,
        *,
        poll_interval: float = POLL_INTERVAL,
        state_dir: Path | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._poll_interval = poll_interval
        self._state_dir = state_dir or _default_state_dir()
        self._log_dir = log_dir or _default_log_dir()

        self._workers: Dict[str, WorkerInfo] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Lazy-loaded supervisor
        self._supervisor = None

    def _get_supervisor(self):
        """Lazy-load WorkflowSupervisor (avoids import cost if never needed)."""
        if self._supervisor is None:
            from workflow_supervisor import WorkflowSupervisor
            self._supervisor = WorkflowSupervisor(
                state_dir=self._state_dir, auto_init=True,
            )
        return self._supervisor

    # ------------------------------------------------------------------
    # Worker registry
    # ------------------------------------------------------------------

    def register(self, terminal_id: str, dispatch_id: str, pid: int | None = None) -> None:
        """Register a worker to be monitored."""
        with self._lock:
            self._workers[terminal_id] = WorkerInfo(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pid=pid,
            )
        logger.info("Health monitor: registered %s (dispatch=%s)", terminal_id, dispatch_id)

    def unregister(self, terminal_id: str) -> None:
        """Remove a worker from monitoring."""
        with self._lock:
            self._workers.pop(terminal_id, None)
        logger.info("Health monitor: unregistered %s", terminal_id)

    def update_last_event(self, terminal_id: str) -> None:
        """Update the last event timestamp for a worker (call on each stream event)."""
        with self._lock:
            worker = self._workers.get(terminal_id)
            if worker:
                worker.last_event_at = time.time()

    def get_workers(self) -> Dict[str, WorkerInfo]:
        """Return a snapshot of all tracked workers."""
        with self._lock:
            return dict(self._workers)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background health polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Health monitor started (interval=%.0fs)", self._poll_interval)

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("Health monitor stopped")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background loop: poll workers every interval."""
        while not self._stop_event.wait(timeout=self._poll_interval):
            self.check_all()

    def check_all(self) -> Dict[str, str]:
        """Run a single health check pass over all registered workers.

        Returns dict of terminal_id → classification.
        """
        with self._lock:
            workers_snapshot = dict(self._workers)

        results: Dict[str, str] = {}
        now = time.time()

        for terminal_id, worker in workers_snapshot.items():
            health = self._adapter.health(terminal_id)
            classification = classify_worker(health, worker, now)
            results[terminal_id] = classification

            self._log_health(terminal_id, classification, health, worker)

            if classification == WorkerClassification.DEAD:
                self._handle_dead(terminal_id, worker, health)
            elif classification == WorkerClassification.HUNG:
                self._handle_hung(terminal_id, worker, health)

        return results

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _handle_dead(self, terminal_id: str, worker: WorkerInfo, health) -> None:
        """Handle a dead (exited) worker process."""
        exit_code = health.details.get("returncode")
        reason = f"Process exited with code {exit_code} for terminal {terminal_id}"
        logger.error("Health monitor: DEAD worker %s — %s", terminal_id, reason)

        try:
            supervisor = self._get_supervisor()
            supervisor.handle_incident(
                incident_class=IncidentClass.PROCESS_CRASH,
                dispatch_id=worker.dispatch_id,
                terminal_id=terminal_id,
                component="subprocess_adapter",
                reason=reason,
                metadata={"exit_code": exit_code, "pid": worker.pid},
            )
        except Exception:
            logger.exception("Failed to report PROCESS_CRASH for %s", terminal_id)

    def _handle_hung(self, terminal_id: str, worker: WorkerInfo, health) -> None:
        """Handle a hung (alive but unresponsive) worker."""
        elapsed = time.time() - worker.last_event_at
        reason = (
            f"Process alive but no output for {elapsed:.0f}s "
            f"(threshold={HUNG_THRESHOLD:.0f}s) on terminal {terminal_id}"
        )
        logger.error("Health monitor: HUNG worker %s — %s", terminal_id, reason)

        try:
            supervisor = self._get_supervisor()
            supervisor.handle_incident(
                incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
                dispatch_id=worker.dispatch_id,
                terminal_id=terminal_id,
                component="subprocess_adapter",
                reason=reason,
                metadata={"elapsed_seconds": elapsed, "pid": worker.pid},
            )
        except Exception:
            logger.exception("Failed to report TERMINAL_UNRESPONSIVE for %s", terminal_id)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_health(self, terminal_id: str, classification: str, health, worker: WorkerInfo) -> None:
        """Append a health check entry to the subprocess health log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "terminal_id": terminal_id,
            "dispatch_id": worker.dispatch_id,
            "classification": classification,
            "process_alive": health.process_alive,
            "pid": health.details.get("pid"),
            "returncode": health.details.get("returncode"),
            "seconds_since_event": time.time() - worker.last_event_at,
        }
        log_path = self._log_dir / "subprocess_health.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.warning("Could not write health log to %s", log_path)


# ---------------------------------------------------------------------------
# CLI entry point (singleton)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VNX Subprocess Health Monitor")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL)
    parser.add_argument("--once", action="store_true", help="Run one check pass and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from subprocess_adapter import SubprocessAdapter
    adapter = SubprocessAdapter()
    monitor = SubprocessHealthMonitor(adapter, poll_interval=args.interval)

    if args.once:
        results = monitor.check_all()
        for tid, cls in results.items():
            print(f"{tid}: {cls}")
    else:
        monitor.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop()
