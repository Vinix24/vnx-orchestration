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

# Helper: verify PAUSED marker exists before attempting resume.
_vnx_resume_validate_marker() {
  local paused_file="$1"
  if [ ! -f "$paused_file" ]; then
    err "[resume] Not paused — ${paused_file} does not exist."
    return 1
  fi
}

# Helper: start all three daemons. Sets _resume_dispatcher_pid and
# _resume_receipt_pid for use by _vnx_resume_verify_readiness.
_vnx_resume_start_daemons() {
  local scripts_dir="$1"
  local logs_dir="$2"

  # Restart dispatcher via supervisor (preferred) or directly
  if [ -f "$scripts_dir/dispatcher_supervisor.sh" ]; then
    log "[resume] Starting dispatcher via dispatcher_supervisor.sh..."
    nohup bash "$scripts_dir/dispatcher_supervisor.sh" \
      > "$logs_dir/dispatcher_supervisor.log" 2>&1 &
    _resume_dispatcher_pid=$!
    log "[resume] dispatcher_supervisor started (PID: $_resume_dispatcher_pid)."
  elif [ -f "$scripts_dir/dispatcher_v8_minimal.sh" ]; then
    log "[resume] Starting dispatcher_v8_minimal.sh directly..."
    nohup bash "$scripts_dir/dispatcher_v8_minimal.sh" \
      > "$logs_dir/dispatcher.log" 2>&1 &
    _resume_dispatcher_pid=$!
    log "[resume] dispatcher started (PID: $_resume_dispatcher_pid)."
  else
    err "[resume] Neither dispatcher_supervisor.sh nor dispatcher_v8_minimal.sh found."
    return 1
  fi

  # Restart receipt_processor via supervisor (preferred) or directly
  if [ -f "$scripts_dir/receipt_processor_supervisor.sh" ]; then
    log "[resume] Starting receipt_processor_supervisor.sh..."
    nohup bash "$scripts_dir/receipt_processor_supervisor.sh" \
      > "$logs_dir/receipt_processor_supervisor.log" 2>&1 &
    _resume_receipt_pid=$!
    log "[resume] receipt_processor_supervisor started (PID: $_resume_receipt_pid)."
  elif [ -f "$scripts_dir/receipt_processor_v4.sh" ]; then
    log "[resume] Starting receipt_processor_v4.sh directly..."
    VNX_MODE=monitor nohup bash "$scripts_dir/receipt_processor_v4.sh" \
      > "$logs_dir/receipt_processor.log" 2>&1 &
    _resume_receipt_pid=$!
    log "[resume] receipt_processor started (PID: $_resume_receipt_pid)."
  else
    err "[resume] Neither receipt_processor_supervisor.sh nor receipt_processor_v4.sh found."
    return 1
  fi

  # Restart queue_watcher — mirrors pause.sh which stops it unconditionally.
  # Use popup watcher when popup is enabled (default), auto-accept otherwise.
  if [ "${VNX_QUEUE_POPUP_ENABLED:-1}" != "0" ]; then
    if [ -f "$scripts_dir/queue_popup_watcher.sh" ]; then
      log "[resume] Starting queue_popup_watcher.sh..."
      nohup bash "$scripts_dir/queue_popup_watcher.sh" \
        > "$logs_dir/queue_watcher.log" 2>&1 &
      log "[resume] queue_watcher started (PID: $!)."
    else
      log "[resume] WARN: queue_popup_watcher.sh not found — queue_watcher skipped."
    fi
  else
    if [ -f "$scripts_dir/queue_auto_accept.sh" ]; then
      log "[resume] Starting queue_auto_accept.sh (queue popup disabled)..."
      nohup bash "$scripts_dir/queue_auto_accept.sh" \
        > "$logs_dir/queue_auto_accept.log" 2>&1 &
      log "[resume] queue_auto_accept started (PID: $!)."
    else
      log "[resume] WARN: queue_auto_accept.sh not found — skipped."
    fi
  fi
}

# Helper: verify mandatory daemons (dispatcher + receipt_processor) are alive.
# Uses _resume_dispatcher_pid and _resume_receipt_pid set by _vnx_resume_start_daemons.
_vnx_resume_verify_readiness() {
  sleep 1
  local resume_failed=0
  if ! kill -0 "$_resume_dispatcher_pid" 2>/dev/null; then
    log "[resume] WARNING: dispatcher did not stay alive (PID: $_resume_dispatcher_pid)."
    resume_failed=1
  fi
  if ! kill -0 "$_resume_receipt_pid" 2>/dev/null; then
    log "[resume] WARNING: receipt_processor did not stay alive (PID: $_resume_receipt_pid)."
    resume_failed=1
  fi
  return $resume_failed
}

# Helper: remove PAUSED marker and append service_resumed NDJSON event.
# Uses python3 json.dumps for safe encoding — prevents injection via $by_dispatch_id.
_vnx_resume_log_lifecycle() {
  local paused_file="$1"
  local lifecycle_log="$2"
  local ts="$3"
  local by_dispatch_id="$4"
  rm -f "$paused_file"
  python3 -c "import json,sys; print(json.dumps({'event_type':'service_resumed','timestamp':sys.argv[1],'by_dispatch_id':sys.argv[2],'reason':'resume'}))" \
    "$ts" "$by_dispatch_id" >> "$lifecycle_log"
}

cmd_resume() {
  local state_dir="${VNX_STATE_DIR:-${VNX_DATA_DIR}/state}"
  local scripts_dir="${VNX_HOME}/scripts"
  local logs_dir="${VNX_LOGS_DIR:-${VNX_DATA_DIR}/logs}"
  local events_dir="${VNX_DATA_DIR}/events"
  local paused_file="$state_dir/PAUSED"
  local lifecycle_log="$events_dir/lifecycle.ndjson"
  local by_dispatch_id="${VNX_DISPATCH_ID:-manual}"

  _vnx_resume_validate_marker "$paused_file" || return 1
  mkdir -p "$events_dir" "$logs_dir"
  _vnx_resume_start_daemons "$scripts_dir" "$logs_dir" || return 1

  if ! _vnx_resume_verify_readiness; then
    err "[resume] One or more mandatory daemons failed to start. PAUSED marker retained."
    return 1
  fi

  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  _vnx_resume_log_lifecycle "$paused_file" "$lifecycle_log" "$ts" "$by_dispatch_id"

  log "[resume] VNX daemons resumed. Dispatcher and receipt_processor restarted."
  return 0
}
