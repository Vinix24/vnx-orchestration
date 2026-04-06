#!/bin/bash
# VNX process lifecycle helper
# Provides PID-safe ownership validation, atomic locking, and graceful stop with fallback.

__VNX_PROC_SHELLOPTS="$(set +o)"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/vnx_paths.sh"

vnx_proc_realpath() {
  local path="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY' "$path"
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
    return 0
  fi
  local dir base
  dir="$(cd "$(dirname "$path")" && pwd -P)"
  base="$(basename "$path")"
  printf '%s/%s\n' "$dir" "$base"
}

vnx_proc_log_event() {
  local log_file="$1"
  local name="$2"
  local event="$3"
  local pid="$4"
  local reason="${5:-}"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  if [ -n "$log_file" ]; then
    printf '[%s] process=%s event=%s pid=%s reason=%s\n' "$ts" "$name" "$event" "$pid" "$reason" >> "$log_file"
  else
    printf '[%s] process=%s event=%s pid=%s reason=%s\n' "$ts" "$name" "$event" "$pid" "$reason"
  fi
}

vnx_proc_cmdline() {
  local pid="$1"
  ps -p "$pid" -o command= 2>/dev/null || true
}

vnx_proc_owner() {
  local pid="$1"
  ps -p "$pid" -o user= 2>/dev/null | awk '{print $1}'
}

vnx_proc_is_owned() {
  local pid="$1"
  local owner
  owner="$(vnx_proc_owner "$pid")"
  [ -n "$owner" ] && [ "$owner" = "$(id -un)" ]
}

vnx_proc_matches_fingerprint() {
  local pid="$1"
  local fingerprint="$2"
  if [ -z "$fingerprint" ]; then
    return 1
  fi
  local cmdline
  cmdline="$(vnx_proc_cmdline "$pid")"
  case "$cmdline" in
    *"$fingerprint"*) return 0 ;;
    *) return 1 ;;
  esac
}

vnx_proc_write_pidfile() {
  local pid_file="$1"
  local pid="$2"
  local fingerprint="$3"
  echo "$pid" > "$pid_file"
  echo "$fingerprint" > "${pid_file}.fingerprint"
}

vnx_proc_read_pidfile() {
  local pid_file="$1"
  local pid=""
  local fingerprint=""
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi
  if [ -f "${pid_file}.fingerprint" ]; then
    fingerprint="$(cat "${pid_file}.fingerprint" 2>/dev/null || true)"
  fi
  printf '%s|%s\n' "$pid" "$fingerprint"
}

vnx_proc_wait_for_exit() {
  local pid="$1"
  local timeout="${2:-5}"
  local elapsed=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$elapsed" -ge "$timeout" ]; then
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 0
}

vnx_proc_stop_pid() {
  local name="$1"
  local pid="$2"
  local fingerprint="$3"
  local log_file="$4"
  local reason="${5:-}"
  local timeout="${6:-5}"

  if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "invalid_pid"
    return 1
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "not_running"
    return 0
  fi

  if ! vnx_proc_is_owned "$pid"; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "owner_mismatch"
    return 1
  fi

  if ! vnx_proc_matches_fingerprint "$pid" "$fingerprint"; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "fingerprint_mismatch"
    return 1
  fi

  vnx_proc_log_event "$log_file" "$name" "stop" "$pid" "$reason"
  kill -TERM "$pid" 2>/dev/null || true

  if vnx_proc_wait_for_exit "$pid" "$timeout"; then
    return 0
  fi

  if ! vnx_proc_is_owned "$pid"; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "owner_mismatch_after_term"
    return 1
  fi
  if ! vnx_proc_matches_fingerprint "$pid" "$fingerprint"; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "fingerprint_mismatch_after_term"
    return 1
  fi

  vnx_proc_log_event "$log_file" "$name" "forced_kill" "$pid" "$reason"
  kill -KILL "$pid" 2>/dev/null || true
  vnx_proc_wait_for_exit "$pid" "$timeout" || true
  return 0
}

vnx_proc_stop_pidfile() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  local reason="${4:-}"
  local timeout="${5:-5}"

  local info
  info="$(vnx_proc_read_pidfile "$pid_file")"
  local pid="${info%%|*}"
  local fingerprint="${info#*|}"

  if vnx_proc_stop_pid "$name" "$pid" "$fingerprint" "$log_file" "$reason" "$timeout"; then
    rm -f "$pid_file" "${pid_file}.fingerprint"
    return 0
  fi
  return 1
}

vnx_proc_find_pids_by_fingerprint() {
  local fingerprint="$1"
  if [ -z "$fingerprint" ]; then
    return 0
  fi
  if [ -n "${VNX_KILL_SCOPE:-}" ]; then
    ps -axo pid=,command= | grep -F "$VNX_KILL_SCOPE" | grep -F "$fingerprint" | grep -v grep | awk '{print $1}' || true
  else
    ps -axo pid=,command= | grep -F "$fingerprint" | grep -v grep | awk '{print $1}' || true
  fi
}

vnx_proc_stop_duplicates() {
  local name="$1"
  local fingerprint="$2"
  local current_pid="$3"
  local log_file="$4"
  local reason="${5:-duplicate}"
  local timeout="${6:-5}"
  local killed=0

  for pid in $(vnx_proc_find_pids_by_fingerprint "$fingerprint"); do
    if [ -n "$current_pid" ] && [ "$pid" = "$current_pid" ]; then
      continue
    fi
    if vnx_proc_stop_pid "$name" "$pid" "$fingerprint" "$log_file" "$reason" "$timeout"; then
      killed=$((killed + 1))
    fi
  done

  if [ "$killed" -gt 0 ]; then
    return 0
  fi
  return 1
}

vnx_proc_acquire_lock() {
  local name="$1"
  local fingerprint="$2"
  local log_file="$3"
  local mode="${4:-exit}"
  local reason="${5:-singleton}"
  local max_age="${VNX_LOCK_MAX_AGE:-3600}"

  mkdir -p "$VNX_LOCKS_DIR" "$VNX_PIDS_DIR"
  local lock_dir="$VNX_LOCKS_DIR/${name}.lock"
  local pid_file="$VNX_PIDS_DIR/${name}.pid"

  if mkdir "$lock_dir" 2>/dev/null; then
    echo "$$" > "$lock_dir/pid"
    echo "$fingerprint" > "$lock_dir/fingerprint"
    date +%s > "$lock_dir/created_at"
    date +%s > "$lock_dir/heartbeat"
    vnx_proc_write_pidfile "$pid_file" "$$" "$fingerprint"
    return 0
  fi

  local existing_pid=""
  local existing_fingerprint=""
  if [ -f "$lock_dir/pid" ]; then
    existing_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
  fi
  if [ -f "$lock_dir/fingerprint" ]; then
    existing_fingerprint="$(cat "$lock_dir/fingerprint" 2>/dev/null || true)"
  fi
  if [ -z "$existing_pid" ] && [ -f "$pid_file" ]; then
    existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi
  if [ -z "$existing_fingerprint" ] && [ -f "${pid_file}.fingerprint" ]; then
    existing_fingerprint="$(cat "${pid_file}.fingerprint" 2>/dev/null || true)"
  fi
  if [ -z "$existing_fingerprint" ]; then
    existing_fingerprint="$fingerprint"
  fi

  # Check for stale lock by age (heartbeat or created_at)
  local lock_ts=0
  if [ -f "$lock_dir/heartbeat" ]; then
    lock_ts="$(cat "$lock_dir/heartbeat" 2>/dev/null || echo 0)"
  elif [ -f "$lock_dir/created_at" ]; then
    lock_ts="$(cat "$lock_dir/created_at" 2>/dev/null || echo 0)"
  fi
  local now_ts
  now_ts="$(date +%s)"
  local lock_age=$(( now_ts - lock_ts ))

  if [ "$lock_age" -ge "$max_age" ] && [ "$lock_ts" -gt 0 ]; then
    vnx_proc_log_event "$log_file" "$name" "stale_lock_expired" "${existing_pid:-unknown}" "age=${lock_age}s max=${max_age}s"
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
      kill -TERM "$existing_pid" 2>/dev/null || true
      vnx_proc_wait_for_exit "$existing_pid" 3 || kill -KILL "$existing_pid" 2>/dev/null || true
    fi
    rm -rf "$lock_dir" "$pid_file" "${pid_file}.fingerprint"
  elif [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    if [ "$mode" = "stop_existing" ]; then
      if vnx_proc_stop_pid "$name" "$existing_pid" "$existing_fingerprint" "$log_file" "$reason" 5; then
        rm -rf "$lock_dir" "$pid_file" "${pid_file}.fingerprint"
      else
        return 1
      fi
    else
      vnx_proc_log_event "$log_file" "$name" "lock_held" "$existing_pid" "$reason"
      return 1
    fi
  else
    rm -rf "$lock_dir" "$pid_file" "${pid_file}.fingerprint"
  fi

  if mkdir "$lock_dir" 2>/dev/null; then
    echo "$$" > "$lock_dir/pid"
    echo "$fingerprint" > "$lock_dir/fingerprint"
    date +%s > "$lock_dir/created_at"
    date +%s > "$lock_dir/heartbeat"
    vnx_proc_write_pidfile "$pid_file" "$$" "$fingerprint"
    return 0
  fi

  vnx_proc_log_event "$log_file" "$name" "lock_failed" "$$" "$reason"
  return 1
}

eval "$__VNX_PROC_SHELLOPTS"
unset __VNX_PROC_SHELLOPTS
