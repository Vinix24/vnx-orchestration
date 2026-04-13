#!/usr/bin/env python3
"""worker_health_monitor.py — Real-time health tracking for headless workers.

Tracks per-dispatch events and reports worker health status.
Writes status to events/worker_health.json every 30s.

BILLING SAFETY: No Anthropic SDK. No api.anthropic.com calls.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Thresholds in seconds
ACTIVE_THRESHOLD = 60.0
SLOW_THRESHOLD = 120.0

# Write interval for worker_health.json
HEALTH_WRITE_INTERVAL = 30.0

# Historical average events per dispatch (used for progress estimation)
# Seeded from observed F46-F50 data; updated each completion
AVG_EVENTS_PER_DISPATCH = 120


class HealthStatus(str, Enum):
    ACTIVE = "active"
    SLOW = "slow"
    STUCK = "stuck"
    COMPLETED = "completed"
    IDLE = "idle"


@dataclass
class WorkerHealth:
    terminal_id: str
    status: HealthStatus
    dispatch_id: str = ""
    event_count: int = 0
    elapsed_seconds: float = 0.0
    last_tool: str = ""
    estimated_progress: float = 0.0
    last_event_age: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        elapsed = self.elapsed_seconds
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return {
            "dispatch_id": self.dispatch_id,
            "status": self.status.value,
            "events": self.event_count,
            "elapsed": f"{minutes}m{seconds:02d}s",
            "last_tool": self.last_tool,
            "estimated_progress": round(self.estimated_progress, 2),
        }


class WorkerHealthMonitor:
    """Real-time health tracking for a single headless worker dispatch."""

    def __init__(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        events_dir: Optional[Path] = None,
        avg_events: int = AVG_EVENTS_PER_DISPATCH,
    ) -> None:
        self.terminal_id = terminal_id
        self.dispatch_id = dispatch_id
        self.avg_events = avg_events

        self._start_time = time.monotonic()
        self._last_event_time = time.monotonic()
        self._event_count = 0
        self._last_tool = ""
        self._completed = False
        self._lock = threading.Lock()

        # Resolve events dir
        if events_dir is not None:
            self._events_dir = events_dir
        else:
            data_dir = os.environ.get("VNX_DATA_DIR", "")
            if data_dir:
                self._events_dir = Path(data_dir) / "events"
            else:
                self._events_dir = (
                    Path(__file__).resolve().parents[2] / ".vnx-data" / "events"
                )

        # Start background writer thread
        self._stop_event = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._write_loop,
            daemon=True,
            name=f"health-writer-{terminal_id}",
        )
        self._writer_thread.start()

    def update(self, event: Any) -> None:
        """Called for each streamed event from the subprocess.

        Accepts StreamEvent objects (with .type / .data) or plain dicts.
        """
        with self._lock:
            self._event_count += 1
            self._last_event_time = time.monotonic()

            # Extract last tool name from tool_use events
            if hasattr(event, "type"):
                etype = event.type
                edata = event.data if isinstance(event.data, dict) else {}
            elif isinstance(event, dict):
                etype = event.get("type", "")
                edata = event.get("data", {})
            else:
                return

            if etype == "tool_use":
                tool_name = edata.get("name", "")
                if tool_name:
                    self._last_tool = tool_name

    def health_status(self) -> WorkerHealth:
        """Return current health assessment."""
        with self._lock:
            if self._completed:
                status = HealthStatus.COMPLETED
                age = 0.0
            else:
                now = time.monotonic()
                age = now - self._last_event_time
                elapsed = now - self._start_time

                if age < ACTIVE_THRESHOLD:
                    status = HealthStatus.ACTIVE
                elif age < SLOW_THRESHOLD:
                    status = HealthStatus.SLOW
                else:
                    status = HealthStatus.STUCK

            elapsed = time.monotonic() - self._start_time
            progress = min(1.0, self._event_count / max(1, self.avg_events))

            return WorkerHealth(
                terminal_id=self.terminal_id,
                status=status,
                dispatch_id=self.dispatch_id,
                event_count=self._event_count,
                elapsed_seconds=elapsed,
                last_tool=self._last_tool,
                estimated_progress=progress,
                last_event_age=age if not self._completed else 0.0,
            )

    def estimated_progress(self) -> float:
        """Return estimated completion fraction 0.0–1.0."""
        with self._lock:
            return min(1.0, self._event_count / max(1, self.avg_events))

    def mark_completed(self) -> None:
        """Signal that the subprocess has exited."""
        with self._lock:
            self._completed = True
        self._stop_event.set()
        # Write final snapshot
        self._write_health_json()

    def stop(self) -> None:
        """Stop background writer thread."""
        self._stop_event.set()

    def _write_loop(self) -> None:
        """Background thread: write health JSON every HEALTH_WRITE_INTERVAL."""
        while not self._stop_event.wait(timeout=HEALTH_WRITE_INTERVAL):
            self._write_health_json()

    def _write_health_json(self) -> None:
        """Write current status to events/worker_health.json (merge with others)."""
        try:
            health_path = self._events_dir / "worker_health.json"
            self._events_dir.mkdir(parents=True, exist_ok=True)

            # Load existing data (other terminals may be writing)
            existing: Dict[str, Any] = {}
            if health_path.exists():
                try:
                    existing = json.loads(health_path.read_text())
                except (json.JSONDecodeError, OSError):
                    existing = {}

            h = self.health_status()
            existing[self.terminal_id] = h.to_dict()

            health_path.write_text(json.dumps(existing, indent=2))
        except Exception as exc:
            logger.debug("worker_health_monitor: failed to write health json: %s", exc)

    def log_stuck_event(self, stuck_log_path: Optional[Path] = None) -> None:
        """Append a stuck-warning entry to events/worker_stuck.ndjson."""
        h = self.health_status()
        if h.status != HealthStatus.STUCK:
            return

        try:
            log_path = stuck_log_path or (self._events_dir / "worker_stuck.ndjson")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "terminal_id": self.terminal_id,
                "dispatch_id": self.dispatch_id,
                "event_count": h.event_count,
                "elapsed": h.elapsed_seconds,
                "last_tool": h.last_tool,
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            logger.warning(
                "Worker %s stuck on dispatch %s (%.0fs no events, %d events total)",
                self.terminal_id,
                self.dispatch_id,
                h.last_event_age,
                h.event_count,
            )
        except Exception as exc:
            logger.debug("worker_health_monitor: failed to log stuck event: %s", exc)
