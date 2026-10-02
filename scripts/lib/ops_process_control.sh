#!/bin/bash
# Shared process-stop helpers for operational scripts.
# Enforces ownership + fingerprint checks and graceful stop with bounded timeout.

# Snapshot the caller's strict mode. Never use `$(set +o)` here: bash clears
# errexit inside a command substitution, so that snapshot always reads "errexit
# off" and the restore below would silently disable `set -e` in the caller.
# `[[ -o ... ]]` reads the option in the current shell.
__VNX_OPS_PROC_ERREXIT=0
if [[ -o errexit ]]; then __VNX_OPS_PROC_ERREXIT=1; fi
__VNX_OPS_PROC_NOUNSET=0
if [[ -o nounset ]]; then __VNX_OPS_PROC_NOUNSET=1; fi
__VNX_OPS_PROC_PIPEFAIL=0
if [[ -o pipefail ]]; then __VNX_OPS_PROC_PIPEFAIL=1; fi

_vnx_ops_proc_restore_strict_mode() {
  if [ "$__VNX_OPS_PROC_ERREXIT" = "1" ]; then set -e; else set +e; fi
  if [ "$__VNX_OPS_PROC_NOUNSET" = "1" ]; then set -u; else set +u; fi
  if [ "$__VNX_OPS_PROC_PIPEFAIL" = "1" ]; then set -o pipefail; else set +o pipefail; fi
}

set -euo pipefail

_OPS_PC_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/process_lifecycle.sh
source "$_OPS_PC_SCRIPT_DIR/process_lifecycle.sh"

vnx_stop_pid_if_matches() {
  local name="$1"
  local pid="$2"
  local log_file="$3"
  local reason="$4"
  local timeout="${5:-3}"
  shift 5
  local fingerprints=("$@")

  if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    return 1
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  if ! vnx_proc_is_owned "$pid"; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "owner_mismatch"
    return 1
  fi

  local fingerprint=""
  local candidate
  for candidate in "${fingerprints[@]}"; do
    if [ -n "$candidate" ] && vnx_proc_matches_fingerprint "$pid" "$candidate"; then
      fingerprint="$candidate"
      break
    fi
  done

  if [ -z "$fingerprint" ]; then
    vnx_proc_log_event "$log_file" "$name" "stop_skipped" "$pid" "fingerprint_mismatch"
    return 1
  fi

  vnx_proc_stop_pid "$name" "$pid" "$fingerprint" "$log_file" "$reason" "$timeout"
}

vnx_stop_by_fingerprints() {
  local name="$1"
  local log_file="$2"
  local reason="$3"
  local timeout="${4:-3}"
  shift 4
  local fingerprints=("$@")
  local stopped=1
  local seen_pids=" "
  local fingerprint pid

  for fingerprint in "${fingerprints[@]}"; do
    [ -n "$fingerprint" ] || continue
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      case "$seen_pids" in
        *" $pid "*) continue ;;
      esac
      seen_pids="${seen_pids}${pid} "
      if vnx_stop_pid_if_matches "$name" "$pid" "$log_file" "$reason" "$timeout" "${fingerprints[@]}"; then
        stopped=0
      fi
    done < <(vnx_proc_find_pids_by_fingerprint "$fingerprint")
  done

  return "$stopped"
}

vnx_stop_listening_port_processes() {
  local name="$1"
  local port="$2"
  local log_file="$3"
  local reason="$4"
  local timeout="${5:-3}"
  shift 5
  local fingerprints=("$@")
  local stopped=1
  local pid

  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    if vnx_stop_pid_if_matches "$name" "$pid" "$log_file" "$reason" "$timeout" "${fingerprints[@]}"; then
      stopped=0
    fi
  done < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)

  return "$stopped"
}

_vnx_ops_proc_restore_strict_mode
unset __VNX_OPS_PROC_ERREXIT __VNX_OPS_PROC_NOUNSET __VNX_OPS_PROC_PIPEFAIL
unset -f _vnx_ops_proc_restore_strict_mode
