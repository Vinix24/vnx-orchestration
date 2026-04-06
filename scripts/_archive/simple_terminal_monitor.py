#!/usr/bin/env python3
"""Simple terminal status monitor - updates dashboard with terminal activity"""
import json
import subprocess
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
from vnx_paths import ensure_env

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DASHBOARD_FILE = STATE_DIR / "dashboard_status.json"
PANES_FILE = STATE_DIR / "panes.json"

def get_terminal_activity():
    """Get terminal activity from tmux"""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command} #{pane_current_path}"],
            capture_output=True, text=True, check=True
        )
        
        activity = {}
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                pane_id, command, path = parts
                activity[pane_id] = {"command": command, "path": path}
        return activity
    except Exception as e:
        print(f"Error getting tmux info: {e}")
        return {}

def update_dashboard():
    """Update dashboard with current terminal status"""
    try:
        # Read current dashboard
        with open(DASHBOARD_FILE) as f:
            dashboard = json.load(f)
        
        # Read panes mapping
        with open(PANES_FILE) as f:
            panes = json.load(f)
        
        # Get tmux activity
        activity = get_terminal_activity()
        
        # Update terminal statuses
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        
        for terminal in ["T0", "T1", "T2", "T3"]:
            pane_id = panes.get(terminal, {}).get("pane_id")
            if pane_id and pane_id in activity:
                info = activity[pane_id]
                if terminal not in dashboard.get("terminals", {}):
                    dashboard.setdefault("terminals", {})[terminal] = {}
                
                dashboard["terminals"][terminal].update({
                    "status": "active" if info["command"] != "zsh" else "idle",
                    "is_active": info["command"] != "zsh",
                    "current_command": info["command"],
                    "last_update": now
                })
        
        # Update timestamp
        dashboard["timestamp"] = now
        
        # Write back
        with open(DASHBOARD_FILE, 'w') as f:
            json.dump(dashboard, f, indent=2)
        
        print(f"✅ Updated dashboard at {now}")
        
    except Exception as e:
        print(f"❌ Error updating dashboard: {e}")

if __name__ == "__main__":
    print("Starting simple terminal monitor...")
    while True:
        update_dashboard()
        time.sleep(5)  # Update every 5 seconds
