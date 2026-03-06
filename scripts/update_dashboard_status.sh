#!/bin/bash

# Update dashboard_status.json with current process statuses

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
STATE_DIR="$VNX_STATE_DIR"
DASHBOARD_FILE="$STATE_DIR/dashboard_status.json"
mkdir -p "$STATE_DIR"

# Get current timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Function to check if process is running
check_process() {
    local pattern="$1"
    if ps aux | grep -E "$pattern" | grep -v grep > /dev/null 2>&1; then
        local pid=$(ps aux | grep -E "$pattern" | grep -v grep | head -1 | awk '{print $2}')
        echo "{\"pid\": \"$pid\", \"running\": true}"
    else
        echo "{\"pid\": \"0\", \"running\": false}"
    fi
}

# Build JSON
cat > "$DASHBOARD_FILE" << EOF
{
    "timestamp": "$TIMESTAMP",
    "processes": {
        "smart_tap": $(check_process "smart_tap_v7_json_translator"),
        "dispatcher": $(check_process "dispatcher_v8_minimal"),
        "queue_watcher": $(check_process "queue_popup_watcher"),
        "receipt_processor": $(check_process "receipt_processor_v4"),
        "supervisor": $(check_process "vnx_supervisor_simple"),
        "heartbeat_ack_monitor": $(check_process "heartbeat_ack_monitor"),
        "intelligence_daemon": $(check_process "intelligence_daemon.py"),
        "unified_state_manager": $(check_process "unified_state_manager_v2"),
        "recommendations_engine": $(check_process "recommendations_engine_daemon")
    },
    "terminals": $(python3 "$SCRIPT_DIR/lib/canonical_state_views.py" --state-dir "$STATE_DIR" dashboard-terminals),
    "gates": {
        "A": "review",
        "B": "testing",
        "C": "investigation"
    },
    "system_status": {
        "health": "operational",
        "receipts_delivered": true,
        "report_processing": true,
        "intelligence": "active"
    }
}
EOF

echo "Dashboard updated at $TIMESTAMP"
echo "File: $DASHBOARD_FILE"
