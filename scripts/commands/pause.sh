#!/usr/bin/env bash
# VNX Command: pause
# Gracefully stops per-project daemons for migration cutover.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, VNX_HOME, VNX_DATA_DIR, etc.) are available
# when this runs.
#
# Creates ${VNX_STATE_DIR}/PAUSED marker and appends service_paused event to
# ${VNX_DATA_DIR}/events/lifecycle.ndjson.
#
# Daemons targeted: dispatcher, receipt_processor, queue_watcher.
# Idempotent: skips already-stopped daemons without error.
# Exit 0: all targeted daemons stopped + marker written.
# Exit 1: one or more daemons could not be stopped (marker NOT written).

# Helper: returns 0 if PID refers to a live (non-zombie) process, 1 otherwise.
# Zombies have exited but wait() hasn't been called on them; kill -0 succeeds for
# them but they cannot perform I/O. Treat them as stopped.
_vnx_proc_is_live() {
  local pid="$1"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  # On macOS/Linux, zombie state is 'Z' in ps -o state
  local state
  state="$(ps -o state= -p "$pid" 2>/dev/null | tr -d '[:space:]')" || true
  if [ "$state" = "Z" ]; then
    return 1
  fi
  return 0
}

# Helper: stop one daemon by PID file. Sets _pause_any_failed=1 on hard failure.
_vnx_pause_stop_daemon() {
  local name="$1"
  local pids_dir="$2"
  local pid_file="$pids_dir/${name}.pid"

  if [ ! -f "$pid_file" ]; then
    log "[pause] $name: no PID file — not running (skipped)."
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  # PID file may have a fingerprint on the same line separated by |
  pid="${pid%%|*}"
  pid="$(printf '%s' "$pid" | tr -d '[:space:]')"

  if [ -z "$pid" ] || ! echo "$pid" | grep -qE '^[0-9]+$'; then
    log "[pause] $name: invalid/empty PID in $pid_file — cleaning up."
    rm -f "$pid_file" "${pid_file}.fingerprint"
    return 0
  fi

  if ! _vnx_proc_is_live "$pid"; then
    log "[pause] $name (PID: $pid): already stopped — cleaning stale PID file."
    rm -f "$pid_file" "${pid_file}.fingerprint"
    return 0
  fi

  log "[pause] Stopping $name (PID: $pid) with SIGTERM..."
  kill -TERM "$pid" 2>/dev/null || true

  local elapsed=0
  while _vnx_proc_is_live "$pid" && [ "$elapsed" -lt 10 ]; do
    sleep 1
    elapsed=$((elapsed + 1))
  done

  if _vnx_proc_is_live "$pid"; then
    log "[pause] $name (PID: $pid): still alive after 10s — sending SIGKILL..."
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi

  if _vnx_proc_is_live "$pid"; then
    log "[pause] ERROR: $name (PID: $pid) still running after SIGKILL."
    _pause_any_failed=1
  else
    rm -f "$pid_file" "${pid_file}.fingerprint"
    log "[pause] $name stopped."
  fi
}

cmd_pause() {
  local state_dir="${VNX_STATE_DIR:-${VNX_DATA_DIR}/state}"
  local pids_dir="${VNX_PIDS_DIR:-${VNX_DATA_DIR}/pids}"
  local events_dir="${VNX_DATA_DIR}/events"
  local paused_file="$state_dir/PAUSED"
  local lifecycle_log="$events_dir/lifecycle.ndjson"
  local by_dispatch_id="${VNX_DISPATCH_ID:-manual}"
  local reason="${1:-migration_cutover}"

  mkdir -p "$state_dir" "$events_dir" "$pids_dir" \
    "${VNX_LOGS_DIR:-${VNX_DATA_DIR}/logs}"

  if [ -f "$paused_file" ]; then
    log "[pause] Already paused — PAUSED marker exists: $paused_file"
    return 0
  fi

  _pause_any_failed=0

  _vnx_pause_stop_daemon "dispatcher" "$pids_dir"
  _vnx_pause_stop_daemon "receipt_processor" "$pids_dir"
  _vnx_pause_stop_daemon "queue_watcher" "$pids_dir"

  if [ "${_pause_any_failed:-0}" -eq 1 ]; then
    err "[pause] Some daemons could not be stopped. PAUSED marker NOT written."
    unset _pause_any_failed
    return 1
  fi
  unset _pause_any_failed

  # Atomic write: tmp file → mv (prevents partial reads)
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local tmp_paused="${paused_file}.tmp.$$"
  printf '{"paused_at":"%s","by_dispatch_id":"%s","reason":"%s"}\n' \
    "$ts" "$by_dispatch_id" "$reason" > "$tmp_paused"
  mv "$tmp_paused" "$paused_file"

  # NDJSON lifecycle event
  printf '{"event_type":"service_paused","timestamp":"%s","by_dispatch_id":"%s","reason":"%s"}\n' \
    "$ts" "$by_dispatch_id" "$reason" >> "$lifecycle_log"

  log "[pause] VNX daemons paused. Marker: $paused_file"
  return 0
}
