#!/usr/bin/env bash
# VNX Command: resume
# Restarts per-project daemons after a vnx pause.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, VNX_HOME, VNX_DATA_DIR, etc.) are available
# when this runs.
#
# Requires ${VNX_STATE_DIR}/PAUSED marker to exist — errors if not paused.
# Appends service_resumed event to ${VNX_DATA_DIR}/events/lifecycle.ndjson.
# Removes PAUSED marker on success.

cmd_resume() {
  local state_dir="${VNX_STATE_DIR:-${VNX_DATA_DIR}/state}"
  local scripts_dir="${VNX_HOME}/scripts"
  local logs_dir="${VNX_LOGS_DIR:-${VNX_DATA_DIR}/logs}"
  local events_dir="${VNX_DATA_DIR}/events"
  local paused_file="$state_dir/PAUSED"
  local lifecycle_log="$events_dir/lifecycle.ndjson"
  local by_dispatch_id="${VNX_DISPATCH_ID:-manual}"

  if [ ! -f "$paused_file" ]; then
    err "[resume] Not paused — ${paused_file} does not exist."
    return 1
  fi

  mkdir -p "$events_dir" "$logs_dir"

  # Restart dispatcher via supervisor (preferred) or directly
  if [ -f "$scripts_dir/dispatcher_supervisor.sh" ]; then
    log "[resume] Starting dispatcher via dispatcher_supervisor.sh..."
    nohup bash "$scripts_dir/dispatcher_supervisor.sh" \
      > "$logs_dir/dispatcher_supervisor.log" 2>&1 &
    log "[resume] dispatcher_supervisor started (PID: $!)."
  elif [ -f "$scripts_dir/dispatcher_v8_minimal.sh" ]; then
    log "[resume] Starting dispatcher_v8_minimal.sh directly..."
    nohup bash "$scripts_dir/dispatcher_v8_minimal.sh" \
      > "$logs_dir/dispatcher.log" 2>&1 &
    log "[resume] dispatcher started (PID: $!)."
  else
    err "[resume] Neither dispatcher_supervisor.sh nor dispatcher_v8_minimal.sh found."
    return 1
  fi

  # Restart receipt_processor via supervisor (preferred) or directly
  if [ -f "$scripts_dir/receipt_processor_supervisor.sh" ]; then
    log "[resume] Starting receipt_processor_supervisor.sh..."
    nohup bash "$scripts_dir/receipt_processor_supervisor.sh" \
      > "$logs_dir/receipt_processor_supervisor.log" 2>&1 &
    log "[resume] receipt_processor_supervisor started (PID: $!)."
  elif [ -f "$scripts_dir/receipt_processor_v4.sh" ]; then
    log "[resume] Starting receipt_processor_v4.sh directly..."
    VNX_MODE=monitor nohup bash "$scripts_dir/receipt_processor_v4.sh" \
      > "$logs_dir/receipt_processor.log" 2>&1 &
    log "[resume] receipt_processor started (PID: $!)."
  else
    err "[resume] Neither receipt_processor_supervisor.sh nor receipt_processor_v4.sh found."
    return 1
  fi

  # Restart queue_auto_accept when queue popup is disabled
  if [ "${VNX_QUEUE_POPUP_ENABLED:-1}" = "0" ]; then
    if [ -f "$scripts_dir/queue_auto_accept.sh" ]; then
      log "[resume] Starting queue_auto_accept.sh..."
      nohup bash "$scripts_dir/queue_auto_accept.sh" \
        > "$logs_dir/queue_auto_accept.log" 2>&1 &
      log "[resume] queue_auto_accept started (PID: $!)."
    else
      log "[resume] WARN: queue_auto_accept.sh not found — skipped."
    fi
  fi

  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # Remove PAUSED marker before writing event (event is the positive confirmation)
  rm -f "$paused_file"

  # NDJSON lifecycle event
  printf '{"event_type":"service_resumed","timestamp":"%s","by_dispatch_id":"%s","reason":"resume"}\n' \
    "$ts" "$by_dispatch_id" >> "$lifecycle_log"

  log "[resume] VNX daemons resumed. Dispatcher and receipt_processor restarted."
  return 0
}
