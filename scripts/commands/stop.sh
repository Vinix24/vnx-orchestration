#!/usr/bin/env bash
# VNX Command: stop
# Extracted from bin/vnx — stops the VNX tmux session and orchestration processes.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, vnx_kill_all_orchestration, VNX_HOME, etc.)
# are available when this runs.

cmd_stop() {
  local session_name="vnx-$(basename "$PROJECT_ROOT")"

  # Stop ALL orchestration processes (comprehensive).
  vnx_kill_all_orchestration "$VNX_HOME/scripts" "$VNX_DATA_DIR/logs" "session_stop"

  # Kill tmux session.
  if tmux has-session -t "$session_name" 2>/dev/null; then
    tmux kill-session -t "$session_name"
    log "VNX session stopped."
  else
    log "No VNX session running."
  fi

  # Export intelligence to git-tracked directory after session cleanup.
  if [ -d "$VNX_INTELLIGENCE_DIR" ] || [ -f "$VNX_STATE_DIR/quality_intelligence.db" ]; then
    log "[stop] Exporting intelligence to $VNX_INTELLIGENCE_DIR"
    cmd_intelligence_export || log "[stop] WARN: intelligence export failed (non-fatal)"
  fi
}
