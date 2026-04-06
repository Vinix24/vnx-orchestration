#!/usr/bin/env python3
"""
Dispatch Lifecycle Tracker - Event Correlation Engine

Tracks complete lifecycle of dispatches from creation to completion,
correlating events across dispatch, ACK, and completion receipts.

Part of VNX Intelligence System Phase 1A
Author: T-MANAGER
Date: 2026-01-07
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import uuid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class DispatchLifecycleTracker:
    """Track complete lifecycle of dispatches with event correlation"""

    def __init__(self, base_dir: Optional[Path] = None):
        """Initialize lifecycle tracker

        Args:
            base_dir: Base directory for VNX system (defaults to project root)
        """
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir / "lib"))
        try:
            from vnx_paths import ensure_env
        except Exception as exc:
            raise SystemExit(f"Failed to load vnx_paths: {exc}")

        paths = ensure_env()
        vnx_home = Path(paths["VNX_HOME"])
        vnx_state = Path(paths["VNX_STATE_DIR"])
        if base_dir is None:
            self.base_dir = Path(paths["PROJECT_ROOT"])
        else:
            self.base_dir = Path(base_dir)

        self.ledger_dir = vnx_state / "ledger"
        self.events_dir = self.ledger_dir / "events"
        self.lifecycles_dir = self.ledger_dir / "lifecycles"
        self.analytics_dir = self.ledger_dir / "analytics"

        # In-memory active lifecycles
        self.active_lifecycles: Dict[str, dict] = {}

        # Load active lifecycles from disk
        self._load_active_lifecycles()

        logger.info(f"[INIT] Lifecycle tracker initialized")
        logger.info(f"[INIT] Ledger: {self.ledger_dir}")
        logger.info(f"[INIT] Active lifecycles: {len(self.active_lifecycles)}")

    def _load_active_lifecycles(self):
        """Load active lifecycles from disk into memory"""
        active_dir = self.lifecycles_dir / "active"

        if not active_dir.exists():
            return

        for lifecycle_file in active_dir.glob("*.json"):
            try:
                with open(lifecycle_file) as f:
                    lifecycle = json.load(f)
                    self.active_lifecycles[lifecycle["dispatch_id"]] = lifecycle
            except Exception as e:
                logger.error(f"[LOAD] Failed to load {lifecycle_file.name}: {e}")

    def _write_event(self, event_type: str, event_data: dict):
        """Write event to appropriate NDJSON file

        Args:
            event_type: Type of event (dispatches, acks, completions, errors)
            event_data: Event data dictionary
        """
        event_file = self.events_dir / f"{event_type}.ndjson"

        try:
            with open(event_file, "a") as f:
                f.write(json.dumps(event_data) + "\n")
        except Exception as e:
            logger.error(f"[EVENT] Failed to write {event_type} event: {e}")

    def _write_lifecycle(self, dispatch_id: str, lifecycle: dict, status: str):
        """Write lifecycle to appropriate directory

        Args:
            dispatch_id: Dispatch identifier
            lifecycle: Lifecycle data
            status: Lifecycle status (active, completed, failed)
        """
        status_dir = self.lifecycles_dir / status
        lifecycle_file = status_dir / f"{dispatch_id}.json"

        try:
            with open(lifecycle_file, "w") as f:
                json.dump(lifecycle, f, indent=2)
        except Exception as e:
            logger.error(f"[LIFECYCLE] Failed to write {status} lifecycle: {e}")

    def _calculate_duration(self, lifecycle: dict) -> Optional[str]:
        """Calculate duration from created to completed

        Args:
            lifecycle: Lifecycle data with timestamps

        Returns:
            Duration string (e.g., "5min 23s") or None
        """
        if "created_at" not in lifecycle or "completed_at" not in lifecycle:
            return None

        try:
            created = datetime.fromisoformat(lifecycle["created_at"].replace("Z", "+00:00"))
            completed = datetime.fromisoformat(lifecycle["completed_at"].replace("Z", "+00:00"))

            delta = completed - created
            total_seconds = int(delta.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                return f"{minutes}min {seconds}s"
            else:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                return f"{hours}h {minutes}min"
        except Exception as e:
            logger.error(f"[DURATION] Failed to calculate: {e}")
            return None

    def _update_analytics(self, lifecycle: dict):
        """Update analytics with completed lifecycle data

        Args:
            lifecycle: Completed lifecycle data
        """
        terminal = lifecycle.get("terminal")
        track = lifecycle.get("track")

        if not terminal or not track:
            return

        # Load existing terminal metrics
        metrics_file = self.analytics_dir / "terminal_metrics.json"

        try:
            if metrics_file.exists():
                with open(metrics_file) as f:
                    metrics = json.load(f)
            else:
                metrics = {}

            # Initialize terminal entry if needed
            if terminal not in metrics:
                metrics[terminal] = {
                    "total_dispatches": 0,
                    "successful": 0,
                    "failed": 0,
                    "total_duration_seconds": 0,
                    "avg_duration_seconds": 0,
                    "success_rate": 0.0
                }

            # Update metrics
            terminal_data = metrics[terminal]
            terminal_data["total_dispatches"] += 1

            if lifecycle["status"] == "success":
                terminal_data["successful"] += 1
            else:
                terminal_data["failed"] += 1

            # Update duration
            if "duration" in lifecycle:
                duration_str = lifecycle["duration"]
                # Parse duration to seconds (simple approximation)
                if "min" in duration_str:
                    parts = duration_str.split()
                    minutes = int(parts[0].replace("min", ""))
                    seconds = int(parts[1].replace("s", "")) if len(parts) > 1 else 0
                    duration_seconds = minutes * 60 + seconds
                else:
                    duration_seconds = int(duration_str.replace("s", ""))

                terminal_data["total_duration_seconds"] += duration_seconds
                terminal_data["avg_duration_seconds"] = (
                    terminal_data["total_duration_seconds"] // terminal_data["total_dispatches"]
                )

            # Calculate success rate
            terminal_data["success_rate"] = (
                terminal_data["successful"] / terminal_data["total_dispatches"]
            )

            # Write updated metrics
            with open(metrics_file, "w") as f:
                json.dump(metrics, f, indent=2)

            logger.info(f"[ANALYTICS] Updated metrics for {terminal}")

        except Exception as e:
            logger.error(f"[ANALYTICS] Failed to update metrics: {e}")

    def track_dispatch(self, dispatch_id: str, metadata: dict):
        """Start tracking new dispatch

        Args:
            dispatch_id: Unique dispatch identifier
            metadata: Dispatch metadata (terminal, track, task_id, etc.)
        """
        logger.info(f"[TRACK] New dispatch: {dispatch_id} → {metadata.get('terminal')}")

        lifecycle = {
            "dispatch_id": dispatch_id,
            "created_at": datetime.utcnow().isoformat() + "Z",  # FIX: Use UTC, not local time
            "terminal": metadata.get("terminal"),
            "track": metadata.get("track"),
            "task_id": metadata.get("task_id"),
            "events": [{
                "type": "dispatch",
                "timestamp": datetime.utcnow().isoformat() + "Z"  # FIX: Use UTC, not local time
            }],
            "status": "pending",
            "estimated_duration": metadata.get("estimated_duration"),
            "dependencies": metadata.get("dependencies", []),
            "quality_context": metadata.get("quality_context", {})
        }

        # Write dispatch event
        event_data = {
            "event_id": str(uuid.uuid4()),
            "event_type": "dispatch",
            "timestamp": lifecycle["created_at"],
            "dispatch_id": dispatch_id,
            **metadata
        }
        self._write_event("dispatches", event_data)

        # Store in active lifecycles
        self.active_lifecycles[dispatch_id] = lifecycle
        self._write_lifecycle(dispatch_id, lifecycle, "active")

        logger.info(f"[TRACK] Lifecycle started for {dispatch_id}")

    def track_ack(self, dispatch_id: str, ack_data: dict):
        """Record ACK receipt for dispatch

        Args:
            dispatch_id: Dispatch identifier
            ack_data: ACK receipt data
        """
        if dispatch_id not in self.active_lifecycles:
            logger.warning(f"[ACK] Unknown dispatch: {dispatch_id}")
            return

        logger.info(f"[ACK] Received for {dispatch_id}")

        lifecycle = self.active_lifecycles[dispatch_id]
        ack_timestamp = datetime.utcnow().isoformat() + "Z"  # FIX: Use UTC, not local time

        lifecycle["ack_time"] = ack_timestamp
        lifecycle["status"] = "acknowledged"
        lifecycle["events"].append({
            "type": "ack",
            "timestamp": ack_timestamp,
            "metadata": ack_data
        })

        # Write ACK event
        event_data = {
            "event_id": str(uuid.uuid4()),
            "event_type": "ack",
            "timestamp": ack_timestamp,
            "dispatch_id": dispatch_id,
            **ack_data
        }
        self._write_event("acks", event_data)

        # Update lifecycle
        self._write_lifecycle(dispatch_id, lifecycle, "active")

        logger.info(f"[ACK] Updated lifecycle for {dispatch_id}")

    def track_completion(self, dispatch_id: str, completion_data: dict):
        """Record completion receipt for dispatch

        Args:
            dispatch_id: Dispatch identifier
            completion_data: Completion receipt data
        """
        if dispatch_id not in self.active_lifecycles:
            logger.warning(f"[COMPLETE] Unknown dispatch: {dispatch_id}")
            return

        logger.info(f"[COMPLETE] Received for {dispatch_id}")

        lifecycle = self.active_lifecycles[dispatch_id]
        completion_timestamp = datetime.utcnow().isoformat() + "Z"  # FIX: Use UTC, not local time

        lifecycle["completed_at"] = completion_timestamp
        lifecycle["status"] = completion_data.get("status", "success")
        lifecycle["confidence"] = completion_data.get("confidence")
        lifecycle["report_path"] = completion_data.get("report_path")
        lifecycle["duration"] = self._calculate_duration(lifecycle)
        lifecycle["events"].append({
            "type": "completion",
            "timestamp": completion_timestamp,
            "metadata": completion_data
        })

        # Write completion event
        event_data = {
            "event_id": str(uuid.uuid4()),
            "event_type": "completion",
            "timestamp": completion_timestamp,
            "dispatch_id": dispatch_id,
            **completion_data
        }
        self._write_event("completions", event_data)

        # Move to completed or failed
        final_status = "completed" if lifecycle["status"] == "success" else "failed"

        # Remove from active
        del self.active_lifecycles[dispatch_id]

        # Remove active file
        active_file = self.lifecycles_dir / "active" / f"{dispatch_id}.json"
        if active_file.exists():
            active_file.unlink()

        # Write to final status
        self._write_lifecycle(dispatch_id, lifecycle, final_status)

        # Update analytics
        self._update_analytics(lifecycle)

        logger.info(f"[COMPLETE] Lifecycle finalized for {dispatch_id} ({final_status})")

    def get_active_count(self) -> int:
        """Get count of active dispatches"""
        return len(self.active_lifecycles)

    def get_lifecycle(self, dispatch_id: str) -> Optional[dict]:
        """Get lifecycle data for specific dispatch

        Args:
            dispatch_id: Dispatch identifier

        Returns:
            Lifecycle data or None if not found
        """
        # Check active first
        if dispatch_id in self.active_lifecycles:
            return self.active_lifecycles[dispatch_id]

        # Check completed
        completed_file = self.lifecycles_dir / "completed" / f"{dispatch_id}.json"
        if completed_file.exists():
            with open(completed_file) as f:
                return json.load(f)

        # Check failed
        failed_file = self.lifecycles_dir / "failed" / f"{dispatch_id}.json"
        if failed_file.exists():
            with open(failed_file) as f:
                return json.load(f)

        return None

    def get_terminal_metrics(self) -> dict:
        """Get aggregated metrics for all terminals

        Returns:
            Dictionary of terminal metrics
        """
        metrics_file = self.analytics_dir / "terminal_metrics.json"

        if metrics_file.exists():
            with open(metrics_file) as f:
                return json.load(f)

        return {}


def main():
    """Test lifecycle tracker with sample data"""

    tracker = DispatchLifecycleTracker()

    logger.info("=" * 60)
    logger.info("Dispatch Lifecycle Tracker - Test Mode")
    logger.info("=" * 60)

    # Test dispatch
    test_dispatch_id = f"20260107-{datetime.now().strftime('%H%M%S')}-test-A"

    tracker.track_dispatch(test_dispatch_id, {
        "terminal": "T1",
        "track": "A",
        "task_id": "test-lifecycle-tracking",
        "estimated_duration": "5-10min",
        "dependencies": [],
        "quality_context": {
            "file_complexity": 5.0,
            "test_coverage": 0.90
        }
    })

    logger.info(f"\n[TEST] Active dispatches: {tracker.get_active_count()}")

    # Simulate ACK
    import time
    time.sleep(2)

    tracker.track_ack(test_dispatch_id, {
        "terminal": "T1",
        "track": "A",
        "terminal_load": {"current_tasks": 1}
    })

    logger.info(f"[TEST] Lifecycle after ACK:")
    lifecycle = tracker.get_lifecycle(test_dispatch_id)
    logger.info(f"  Status: {lifecycle['status']}")
    logger.info(f"  Events: {len(lifecycle['events'])}")

    # Simulate completion
    time.sleep(3)

    tracker.track_completion(test_dispatch_id, {
        "status": "success",
        "confidence": 0.95,
        "report_path": "/test/report.md"
    })

    logger.info(f"\n[TEST] Active dispatches after completion: {tracker.get_active_count()}")

    # Get final lifecycle
    final_lifecycle = tracker.get_lifecycle(test_dispatch_id)
    logger.info(f"[TEST] Final lifecycle:")
    logger.info(f"  Status: {final_lifecycle['status']}")
    logger.info(f"  Duration: {final_lifecycle['duration']}")
    logger.info(f"  Events: {len(final_lifecycle['events'])}")

    # Get metrics
    metrics = tracker.get_terminal_metrics()
    logger.info(f"\n[TEST] Terminal metrics:")
    for terminal, data in metrics.items():
        logger.info(f"  {terminal}: {data['total_dispatches']} dispatches, "
                   f"{data['success_rate']:.2%} success rate, "
                   f"{data['avg_duration_seconds']}s avg duration")

    logger.info("\n[TEST] Test complete")


if __name__ == "__main__":
    main()
