#!/usr/bin/env bash
# VNX Command: recover
# Recovers a VNX session from unclean shutdown or stale state.
#
# PR-5: When runtime core is active (VNX_RUNTIME_PRIMARY=1), recovery uses
# the canonical runtime recovery engine (vnx_recover_runtime.py) which
# reconciles leases, incidents, and tmux bindings from canonical state.
#
# Legacy recovery (lock/PID/dispatch-file cleanup) runs as fallback when
# runtime core is inactive or as a complement to runtime recovery.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_recover() {
  local aggressive=false
  local dry_run=false
  local json_output=false
  local legacy_only=false

  while [ $# -gt 0 ]; do
    case "$1" in
      --aggressive)
        aggressive=true; shift ;;
      --dry-run)
        dry_run=true; shift ;;
      --json)
        json_output=true; shift ;;
      --legacy)
        legacy_only=true; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx recover [options]

Recovers a VNX session from unclean shutdown or stale state.

When runtime core is active (default after FP-B cutover), recovery uses
the canonical runtime recovery engine which reconciles:
  - Terminal leases (expire stale, recover expired, release orphans)
  - Dispatch state (timeout stuck, flag for review)
  - Incident log (summarize, resolve stale, reset budgets)
  - tmux bindings (verify profile, remap stale panes)

Legacy cleanup (locks, PIDs, dispatch files) always runs as a complement.

Options:
  --aggressive     Force-kill all scoped VNX processes before recovery
  --dry-run        Show what would be recovered without making changes
  --json           Output runtime recovery report as JSON
  --legacy         Skip runtime recovery, only run legacy cleanup
  -h, --help       Show this help

Rollback guidance:
  If runtime recovery causes issues, disable it with:
    python scripts/rollback_runtime_core.py rollback
  Then run: vnx recover --legacy
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

  local runtime_exit=0

  # ── Runtime recovery (canonical state) ──────────────────────────────────
  if [ "$legacy_only" != true ] && [ "${VNX_RUNTIME_PRIMARY:-1}" = "1" ]; then
    local state_dir="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}"
    local scripts_lib="$VNX_HOME/scripts/lib"

    if [ -f "$scripts_lib/vnx_recover_runtime.py" ] && command -v python3 &>/dev/null; then
      log "[recover] Running canonical runtime recovery..."

      local runtime_args=("--state-dir" "$state_dir")
      [ "$dry_run" = true ] && runtime_args+=("--dry-run")
      [ "$json_output" = true ] && runtime_args+=("--json")

      PYTHONPATH="$scripts_lib${PYTHONPATH:+:$PYTHONPATH}" \
        python3 "$scripts_lib/vnx_recover_runtime.py" "${runtime_args[@]}"
      runtime_exit=$?

      if [ "$runtime_exit" -eq 0 ]; then
        log "[recover] Runtime recovery: clean or recovered"
      elif [ "$runtime_exit" -eq 2 ]; then
        log "[recover] Runtime recovery: blocked — see report above"
      else
        log "[recover] Runtime recovery: partial — some issues remain"
      fi

      log ""
    else
      log "[recover] Runtime recovery engine not available — falling back to legacy"
    fi
  else
    if [ "$legacy_only" = true ]; then
      log "[recover] Legacy-only mode (--legacy)"
    else
      log "[recover] Runtime core inactive — using legacy recovery"
    fi
  fi

  # ── Legacy recovery (file-based cleanup) ─────────────────────────────────
  # PR-3: Delegated to Python module (vnx_recover_legacy.py) for testability.
  # Falls back to inline bash if Python is unavailable.
  log "[recover] Running legacy cleanup..."

  local recovered=0
  local issues=0
  local scripts_lib="$VNX_HOME/scripts/lib"
  local _legacy_exit=0

  if [ -f "$scripts_lib/vnx_recover_legacy.py" ] && command -v python3 &>/dev/null; then
    local legacy_args=(
      "--locks-dir" "${VNX_LOCKS_DIR:-$VNX_DATA_DIR/locks}"
      "--pids-dir" "${VNX_PIDS_DIR:-$VNX_DATA_DIR/pids}"
      "--dispatch-dir" "${VNX_DISPATCH_DIR:-$VNX_DATA_DIR/dispatches}"
      "--state-dir" "${VNX_STATE_DIR:-$VNX_DATA_DIR/state}"
      "--data-dir" "${VNX_DATA_DIR:-}"
      "--max-lock-age" "${VNX_LOCK_MAX_AGE:-3600}"
    )
    [ "$dry_run" = true ] && legacy_args+=("--dry-run")
    [ "$legacy_only" = true ] && legacy_args+=("--legacy-only")
    [ "${VNX_RUNTIME_PRIMARY:-1}" = "1" ] && legacy_args+=("--runtime-primary")

    PYTHONPATH="$scripts_lib${PYTHONPATH:+:$PYTHONPATH}" \
      python3 "$scripts_lib/vnx_recover_legacy.py" "${legacy_args[@]}"
    _legacy_exit=$?
  else
    log "[recover] Python legacy cleanup not available — using inline fallback"
    # Minimal inline fallback: just clear unclean marker and stale PIDs
    local unclean_marker="${VNX_LOCKS_DIR:-}/.unclean_shutdown"
    if [ -f "$unclean_marker" ]; then
      if [ "$dry_run" != true ]; then
        rm -f "$unclean_marker"
        log "  CLEARED: unclean-shutdown marker"
        recovered=1
      fi
    fi
  fi

  # ── Aggressive mode (shell-level, not in Python) ─────────────────────
  # Process killing via pgrep/pkill stays in shell since it needs the
  # vnx_kill_all_orchestration function and shell process context.
  if [ "$aggressive" = true ]; then
    log "[recover] Aggressive mode: killing all scoped VNX processes..."
    if [ "$dry_run" = true ]; then
      local orphan_count
      orphan_count="$(pgrep -f "${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}" 2>/dev/null | grep -v "^$$\$" | wc -l | tr -d ' ')"
      log "  WOULD KILL: $orphan_count process(es) scoped to $VNX_DATA_DIR"
    else
      local _scripts_dir="$VNX_HOME/scripts"
      vnx_kill_all_orchestration "$_scripts_dir" "${VNX_LOGS_DIR:-$VNX_DATA_DIR/logs}" "recovery"
      log "  KILLED: All scoped orchestration processes"
    fi
  fi

  # ── Summary ───────────────────────────────────────────────────────────
  log ""
  log "════════════════════════════════════════════════════════════════"

  if [ "${VNX_RUNTIME_PRIMARY:-1}" = "1" ] && [ "$legacy_only" != true ]; then
    log ""
    log " Runtime core: ACTIVE — canonical recovery ran above"
    log " Rollback: python scripts/rollback_runtime_core.py rollback"
  fi

  log "════════════════════════════════════════════════════════════════"

  if [ "$runtime_exit" -eq 0 ]; then
    log ""
    log " Session should be usable. Run 'vnx start' to restart."
  fi

  return "$runtime_exit"
}
