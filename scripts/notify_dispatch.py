#!/usr/bin/env python3
"""
Notify Dispatch - Signal heartbeat_ack_monitor.py about new dispatches
Usage: python notify_dispatch.py <dispatch_id> <terminal> <task_id> [pr_id]
"""

import sys
import socket
import json
from datetime import datetime, timezone

def notify_dispatch(dispatch_id: str, terminal: str, task_id: str, pr_id: str = ''):
    """Send dispatch notification to heartbeat monitor via Unix socket"""

    # Project-scoped socket to prevent cross-project receipt contamination
    import os
    project_root = os.environ.get('PROJECT_ROOT', os.getcwd())
    project_name = os.path.basename(project_root)
    socket_path = f'/tmp/heartbeat_ack_monitor_{project_name}.sock'

    message = {
        'action': 'track_dispatch',
        'dispatch_id': dispatch_id,
        'terminal': terminal,
        'task_id': task_id,
        'pr_id': pr_id,  # SPRINT 2: Add PR-ID for auto-completion matching
        'sent_time': datetime.now(timezone.utc).isoformat()  # UTC with timezone info
    }

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.send(json.dumps(message).encode() + b'\n')

        # Wait for confirmation
        response = client.recv(1024).decode().strip()
        client.close()

        if response == 'OK':
            print(f"✓ Notified heartbeat monitor: {dispatch_id} → {terminal}")
            return True
        else:
            print(f"✗ Heartbeat monitor responded: {response}", file=sys.stderr)
            return False

    except FileNotFoundError:
        print(f"✗ Heartbeat monitor socket not found: {socket_path}", file=sys.stderr)
        print("  Heartbeat monitor may not be running", file=sys.stderr)
        return False
    except Exception as e:
        print(f"✗ Failed to notify heartbeat monitor: {e}", file=sys.stderr)
        return False

if __name__ == '__main__':
    if len(sys.argv) < 4 or len(sys.argv) > 5:
        print(f"Usage: {sys.argv[0]} <dispatch_id> <terminal> <task_id> [pr_id]", file=sys.stderr)
        sys.exit(1)

    dispatch_id = sys.argv[1]
    terminal = sys.argv[2]
    task_id = sys.argv[3]
    pr_id = sys.argv[4] if len(sys.argv) == 5 else ''

    success = notify_dispatch(dispatch_id, terminal, task_id, pr_id)
    sys.exit(0 if success else 1)
