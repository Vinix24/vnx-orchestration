#!/usr/bin/env python3
"""
Lifecycle Tracker Notification Client

Notifies the dispatch_lifecycle_tracker about new dispatches.
Called by dispatcher after sending dispatch to terminal.

Author: T-MANAGER
Date: 2026-01-07
"""

import sys
from pathlib import Path

def notify_lifecycle_tracker(dispatch_id: str, terminal: str, track: str, task_id: str):
    """Notify lifecycle tracker about new dispatch

    Args:
        dispatch_id: Dispatch identifier
        terminal: Target terminal (T1, T2, T3)
        track: Track letter (A, B, C)
        task_id: Task identifier
    """
    try:
        # Import lifecycle tracker
        vnx_home = Path(os.environ.get("VNX_HOME", Path(__file__).resolve().parents[1]))
        project_root = vnx_home.parent.parent
        sys.path.insert(0, str(vnx_home / "scripts"))

        from dispatch_lifecycle_tracker import DispatchLifecycleTracker

        # Create tracker instance
        tracker = DispatchLifecycleTracker(base_dir=project_root)

        # Track dispatch
        tracker.track_dispatch(dispatch_id, {
            "terminal": terminal,
            "track": track,
            "task_id": task_id,
            "estimated_duration": "unknown",  # Will be enhanced later
            "dependencies": [],
            "quality_context": {}
        })

        print(f"✓ Lifecycle tracker notified: {dispatch_id} → {terminal}")
        return True

    except Exception as e:
        print(f"✗ Failed to notify lifecycle tracker: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: notify_lifecycle_tracker.py <dispatch_id> <terminal> <track> <task_id>")
        sys.exit(1)

    dispatch_id = sys.argv[1]
    terminal = sys.argv[2]
    track = sys.argv[3]
    task_id = sys.argv[4]

    success = notify_lifecycle_tracker(dispatch_id, terminal, track, task_id)
    sys.exit(0 if success else 1)
