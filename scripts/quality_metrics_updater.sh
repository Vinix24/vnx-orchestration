#!/bin/bash
# Quality Metrics Updater Service
# Periodically updates dashboard with quality intelligence metrics

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"

# SPRINT 1: Singleton enforcement to prevent duplicate processes
source "$SCRIPT_DIR/singleton_enforcer.sh"
enforce_singleton "quality_metrics_updater.sh"

VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
DASHBOARD_FILE="$STATE_DIR/dashboard_status.json"
TEMP_DASHBOARD="$STATE_DIR/dashboard_status.json.tmp"
UPDATE_INTERVAL=30  # Update every 30 seconds

# Python venv activation (dynamic resolution)
_activate_venv || echo "[quality_metrics_updater] WARN: No venv found, using system python"

echo "[$(date)] Quality Metrics Updater starting..."
echo "[$(date)] Update interval: ${UPDATE_INTERVAL}s"

while true; do
    # Run quality dashboard integration silently
    python3 "$VNX_DIR/scripts/quality_dashboard_integration.py" > /dev/null 2>&1

    if [ $? -eq 0 ]; then
        echo "[$(date)] ✓ Quality metrics updated"
    else
        echo "[$(date)] ✗ Quality metrics update failed"
    fi

    sleep $UPDATE_INTERVAL
done
