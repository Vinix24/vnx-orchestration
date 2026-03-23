#!/usr/bin/env bash
# VNX Command: recover
# Recovers a VNX session from unclean shutdown or stale state.
#
# Detects and resolves: stale locks, orphan processes, incomplete dispatches,
# stale terminal claims, and unclean-shutdown markers.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_recover() {
  local aggressive=false
  local dry_run=false

  while [ $# -gt 0 ]; do
    case "$1" in
      --aggressive)
        aggressive=true; shift ;;
      --dry-run)
        dry_run=true; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx recover [options]

Recovers a VNX session from unclean shutdown or stale state.

Options:
  --aggressive     Force-kill all scoped VNX processes before recovery
  --dry-run        Show what would be recovered without making changes
  -h, --help       Show this help

Recovery actions:
  1. Detect and clear stale process locks (age-based + PID check)
  2. Kill orphan VNX processes scoped to this project
  3. Move incomplete dispatches from active/ to failed/
  4. Reset stale terminal claims to idle
  5. Clear unclean-shutdown marker
  6. Clean up stale payload temp files
HELP
        return 0
        ;;
      -*)
        err "[recover] Unknown option: $1"
        return 1
        ;;
      *)
        err "[recover] Unexpected argument: $1"
        return 1
        ;;
    esac
  done

  log "[recover] Starting recovery scan..."

  local recovered=0
  local issues=0

  # ── Step 1: Detect and clear stale locks ───────────────────────────────
  log "[recover] Checking for stale locks..."
  local max_age="${VNX_LOCK_MAX_AGE:-3600}"

  if [ -d "${VNX_LOCKS_DIR:-}" ]; then
    local lock_dir
    for lock_dir in "$VNX_LOCKS_DIR"/*.lock; do
      [ -d "$lock_dir" ] || continue
      local lock_name
      lock_name="$(basename "$lock_dir" .lock)"
      local lock_pid=""
      [ -f "$lock_dir/pid" ] && lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"

      local stale_reason=""

      # Check 1: PID not running
      if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
        stale_reason="process_dead"
      fi

      # Check 2: Lock age exceeds max
      if [ -z "$stale_reason" ]; then
        local lock_ts=0
        [ -f "$lock_dir/heartbeat" ] && lock_ts="$(cat "$lock_dir/heartbeat" 2>/dev/null || echo 0)"
        [ "$lock_ts" -eq 0 ] && [ -f "$lock_dir/created_at" ] && lock_ts="$(cat "$lock_dir/created_at" 2>/dev/null || echo 0)"
        local now_ts
        now_ts="$(date +%s)"
        local age=$(( now_ts - lock_ts ))
        if [ "$lock_ts" -gt 0 ] && [ "$age" -ge "$max_age" ]; then
          stale_reason="expired (age=${age}s, max=${max_age}s)"
        fi
      fi

      # Check 3: No PID file at all (orphan lock dir)
      if [ -z "$stale_reason" ] && [ -z "$lock_pid" ]; then
        stale_reason="orphan_lock (no PID)"
      fi

      if [ -n "$stale_reason" ]; then
        issues=$((issues + 1))
        if [ "$dry_run" = true ]; then
          log "  WOULD CLEAR: $lock_name ($stale_reason, PID: ${lock_pid:-none})"
        else
          # Kill the process if still running (expired case)
          if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
            kill -TERM "$lock_pid" 2>/dev/null || true
            sleep 1
            kill -0 "$lock_pid" 2>/dev/null && kill -KILL "$lock_pid" 2>/dev/null || true
          fi
          rm -rf "$lock_dir"
          rm -f "$VNX_PIDS_DIR/${lock_name}.pid" "$VNX_PIDS_DIR/${lock_name}.pid.fingerprint" 2>/dev/null || true
          log "  CLEARED: $lock_name ($stale_reason, PID: ${lock_pid:-none})"
          recovered=$((recovered + 1))
        fi
      fi
    done
  fi

  # ── Step 2: Kill orphan processes (aggressive mode) ────────────────────
  if [ "$aggressive" = true ]; then
    log "[recover] Aggressive mode: killing all scoped VNX processes..."
    if [ "$dry_run" = true ]; then
      local orphan_count
      orphan_count="$(pgrep -f "${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}" 2>/dev/null | grep -v "^$$\$" | wc -l | tr -d ' ')"
      log "  WOULD KILL: $orphan_count process(es) scoped to $VNX_DATA_DIR"
      issues=$((issues + orphan_count))
    else
      local scripts_dir="$VNX_HOME/scripts"
      vnx_kill_all_orchestration "$scripts_dir" "${VNX_LOGS_DIR:-$VNX_DATA_DIR/logs}" "recovery"
      log "  KILLED: All scoped orchestration processes"
      recovered=$((recovered + 1))
    fi
  else
    # Non-aggressive: only kill processes with stale PID files
    if [ -d "${VNX_PIDS_DIR:-}" ]; then
      local pid_file
      for pid_file in "$VNX_PIDS_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        local pid
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        local proc_name
        proc_name="$(basename "${pid_file%.pid}")"

        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
          if [ "$dry_run" = true ]; then
            log "  WOULD CLEAN: stale PID file for $proc_name (PID: $pid, not running)"
          else
            rm -f "$pid_file" "${pid_file}.fingerprint"
            log "  CLEANED: stale PID file for $proc_name (PID: $pid)"
            recovered=$((recovered + 1))
          fi
          issues=$((issues + 1))
        fi
      done
    fi
  fi

  # ── Step 3: Move incomplete dispatches to failed/ ──────────────────────
  log "[recover] Checking for incomplete dispatches..."
  local active_dir="${VNX_DISPATCH_DIR:-$VNX_DATA_DIR/dispatches}/active"
  local failed_dir="${VNX_DISPATCH_DIR:-$VNX_DATA_DIR/dispatches}/failed"

  if [ -d "$active_dir" ]; then
    local dispatch_file
    for dispatch_file in "$active_dir"/*.md; do
      [ -f "$dispatch_file" ] || continue
      issues=$((issues + 1))
      local dispatch_name
      dispatch_name="$(basename "$dispatch_file")"

      if [ "$dry_run" = true ]; then
        log "  WOULD MOVE: $dispatch_name → failed/ (incomplete)"
      else
        mkdir -p "$failed_dir"
        mv "$dispatch_file" "$failed_dir/${dispatch_name%.md}.recovered.md"
        log "  MOVED: $dispatch_name → failed/ (recovered)"
        recovered=$((recovered + 1))
      fi
    done
  fi

  # ── Step 4: Reset stale terminal claims ────────────────────────────────
  log "[recover] Checking terminal claims..."
  local terminal_state="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}/terminal_state.json"

  if [ -f "$terminal_state" ] && command -v python3 &>/dev/null; then
    local stale_claims
    stale_claims="$(python3 -c "
import json, sys
try:
    with open('$terminal_state') as f:
        data = json.load(f)
    stale = []
    for tid, info in data.items():
        if info.get('status') == 'working':
            stale.append(tid)
    print('|'.join(stale))
except Exception:
    print('')
" 2>/dev/null)" || true

    if [ -n "$stale_claims" ]; then
      IFS='|' read -ra claim_terminals <<< "$stale_claims"
      for tid in "${claim_terminals[@]}"; do
        [ -n "$tid" ] || continue
        issues=$((issues + 1))
        if [ "$dry_run" = true ]; then
          log "  WOULD RESET: $tid claim (working → idle)"
        else
          python3 -c "
import json
with open('$terminal_state') as f:
    data = json.load(f)
if '$tid' in data:
    data['$tid']['status'] = 'idle'
    data['$tid']['claimed_by'] = None
with open('$terminal_state', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || true
          log "  RESET: $tid claim (working → idle)"
          recovered=$((recovered + 1))
        fi
      done
    fi
  fi

  # ── Step 5: Clear unclean-shutdown marker ──────────────────────────────
  local unclean_marker="${VNX_LOCKS_DIR:-}/.unclean_shutdown"
  if [ -f "$unclean_marker" ]; then
    issues=$((issues + 1))
    if [ "$dry_run" = true ]; then
      log "  WOULD CLEAR: unclean-shutdown marker ($(cat "$unclean_marker" | head -1))"
    else
      rm -f "$unclean_marker"
      log "  CLEARED: unclean-shutdown marker"
      recovered=$((recovered + 1))
    fi
  fi

  # ── Step 6: Clean up stale payload temp files ──────────────────────────
  local payload_dir="${VNX_DATA_DIR:-}/dispatch_payloads"
  if [ -d "$payload_dir" ]; then
    local stale_payloads
    stale_payloads="$(find "$payload_dir" -name 'payload_*.txt' -type f -mmin +60 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$stale_payloads" -gt 0 ]; then
      issues=$((issues + stale_payloads))
      if [ "$dry_run" = true ]; then
        log "  WOULD CLEAN: $stale_payloads stale payload temp file(s)"
      else
        find "$payload_dir" -name 'payload_*.txt' -type f -mmin +60 -delete 2>/dev/null || true
        log "  CLEANED: $stale_payloads stale payload temp file(s)"
        recovered=$((recovered + stale_payloads))
      fi
    fi
  fi

  # ── Summary ───────────────────────────────────────────────────────────
  log ""
  log "════════════════════════════════════════════════════════════════"
  if [ "$dry_run" = true ]; then
    log " Recovery scan (dry-run): $issues issue(s) found"
    log " Run without --dry-run to apply fixes."
  elif [ "$recovered" -gt 0 ]; then
    log " Recovery complete: $recovered issue(s) resolved"
    log ""
    log " Session should be usable. Run 'vnx start' to restart."
  else
    log " No recovery needed: session state is clean"
  fi
  log "════════════════════════════════════════════════════════════════"

  return 0
}
