#!/usr/bin/env bash
# Shared utilities for VNX hooks.

_VNX_HOOK_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_VNX_HOOK_LIB_DIR/../../scripts/lib/vnx_paths.sh"

vnx_detect_terminal() {
  case "$PWD" in
    */terminals/T0|*/T0) echo "T0" ;;
    */terminals/T1|*/T1) echo "T1" ;;
    */terminals/T2|*/T2) echo "T2" ;;
    */terminals/T3|*/T3) echo "T3" ;;
    */terminals/T-MANAGER|*/T-MANAGER) echo "T-MANAGER" ;;
    *) echo "unknown" ;;
  esac
}

vnx_log() {
  mkdir -p "${VNX_LOGS_DIR:-}" 2>/dev/null || true
  echo "[VNX:hook $(date +%H:%M:%S)] $*" >> "${VNX_LOGS_DIR:-/dev/null}/hook_events.log" 2>/dev/null || true
}

vnx_json_context() {
  local context="$1"
  local event="${2:-Stop}"

  if command -v jq >/dev/null 2>&1; then
    echo "$context" | jq -Rs --arg event "$event" \
      '{hookSpecificOutput:{hookEventName:$event,additionalContext:.}}'
    return 0
  fi

  local escaped
  escaped="$(echo "$context" | sed 's/\\/\\\\/g;s/"/\\"/g' | tr '\n' ' ')"
  printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}' "$event" "$escaped"
}

vnx_acquire_lock() {
  local name="$1"
  local ttl="${2:-300}"
  local now created_at age
  local lock_dir ts_file

  mkdir -p "$VNX_LOCKS_DIR" 2>/dev/null || true
  lock_dir="$VNX_LOCKS_DIR/${name}.lock"

  if mkdir "$lock_dir" 2>/dev/null; then
    date +%s > "$lock_dir/created_at"
    return 0
  fi

  ts_file="$lock_dir/created_at"
  if [[ -f "$ts_file" ]]; then
    created_at="$(cat "$ts_file" 2>/dev/null || echo "0")"
    if [[ ! "$created_at" =~ ^[0-9]+$ ]]; then
      created_at=0
    fi
    now="$(date +%s)"
    age=$(( now - created_at ))

    if (( age > ttl )); then
      vnx_log "Stale lock removed: $name (age=${age}s > ttl=${ttl}s)"
      rm -rf "$lock_dir"
      if mkdir "$lock_dir" 2>/dev/null; then
        date +%s > "$lock_dir/created_at"
        return 0
      fi
    fi
  else
    rm -rf "$lock_dir"
    if mkdir "$lock_dir" 2>/dev/null; then
      date +%s > "$lock_dir/created_at"
      return 0
    fi
  fi

  return 1
}

vnx_release_lock() {
  local name="$1"
  rm -rf "$VNX_LOCKS_DIR/${name}.lock"
}

vnx_context_rotation_enabled() {
  [[ "${VNX_CONTEXT_ROTATION_ENABLED:-0}" == "1" ]]
}
