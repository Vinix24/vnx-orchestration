#!/bin/bash
# dispatcher_supervisor_ticks.sh — Unified supervisor tick helpers for the dispatcher.
#
# Source this from dispatcher_minimal.sh after dispatch_logging.sh is loaded.
#
# Required bindings (provided by the sourcing dispatcher):
#   log()            — from scripts/lib/dispatch_logging.sh
#   STATE_DIR        — dispatcher state directory (VNX_STATE_DIR)
#   VNX_LOGS_DIR     — log directory
#   VNX_DIR          — VNX home directory
#   VNX_DATA_DIR     — VNX data directory
#   SCRIPT_DIR       — directory containing the dispatcher script
#
# Environment variables (optional, with defaults):
#   VNX_SUPERVISOR_MODE            — "unified" enables ticks; default is "legacy" (no-op)
#   VNX_RUNTIME_SUPERVISE_INTERVAL — seconds between supervise_all() calls; default 60
#   VNX_LEASE_SWEEP_INTERVAL_SEC   — seconds between lease_sweep calls; default 30
#   VNX_LEARNING_ENABLED           — "1" enables the daily learning cycle tick; default 0 (off)
#   VNX_LEARNING_CYCLE_INTERVAL    — seconds between learning cycle runs; default 86400 (daily)

# _maybe_runtime_supervise — throttled RuntimeSupervisor.supervise_all() tick (SUP-PR3).
# Invokes scripts/lib/runtime_supervise.py at most once per
# VNX_RUNTIME_SUPERVISE_INTERVAL seconds when VNX_SUPERVISOR_MODE=unified.
# Default legacy mode is bit-identical (returns 0 immediately).
_maybe_runtime_supervise() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
    local interval="${VNX_RUNTIME_SUPERVISE_INTERVAL:-60}"
    local state_file="$STATE_DIR/.last_runtime_supervise_ts"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if (( now - last < interval )); then
        return 0
    fi
    local log_file="$VNX_LOGS_DIR/runtime_supervise.log"
    mkdir -p "$(dirname "$log_file")"
    python3 "$VNX_DIR/scripts/lib/runtime_supervise.py" >> "$log_file" 2>&1 || true
    echo "$now" > "$state_file"
}

# _maybe_auto_seed_tracks — flag-gated planning auto-seed tick.
# When VNX_AUTO_SEED_TRACKS=1, run the idempotent `vnx objective sync --apply`
# once per prelude tick to keep tracks current with ROADMAP.yaml. Best-effort,
# non-blocking, logged. Default (unset) = no behaviour change. This NEVER writes
# ROADMAP.yaml and NEVER promotes deliverables (the human gate is preserved).
_maybe_auto_seed_tracks() {
    [[ "${VNX_AUTO_SEED_TRACKS:-0}" == "1" ]] || return 0
    local log_file="$VNX_LOGS_DIR/auto_seed_tracks.log"
    mkdir -p "$(dirname "$log_file")"
    python3 "$SCRIPT_DIR/planning_cli.py" objective sync --apply \
        >> "$log_file" 2>&1 || true
}

# _maybe_objective_reconcile — throttled advisory-first git-grounded reconcile tick (D4).
# Invokes `objective reconcile` at most once per VNX_OBJECTIVE_RECONCILE_INTERVAL seconds
# when VNX_SUPERVISOR_MODE=unified. Default CHECK mode only; --apply added when
# VNX_AUTO_CLOSE=1 (operator must set this after `objective reconcile-streak` confirms the
# flip criterion). Best-effort (|| true); logged to objective_reconcile.log.
# Default (unset/legacy) = no behaviour change.
_maybe_objective_reconcile() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
    local interval="${VNX_OBJECTIVE_RECONCILE_INTERVAL:-900}"
    local state_file="$STATE_DIR/.last_objective_reconcile_ts"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if (( now - last < interval )); then
        return 0
    fi
    local log_file="$VNX_LOGS_DIR/objective_reconcile.log"
    mkdir -p "$(dirname "$log_file")"
    local -a cmd=(
        python3 "$VNX_DIR/scripts/planning_cli.py"
        objective reconcile
        --project-id "$VNX_PROJECT_ID"
        --state-dir "$STATE_DIR"
    )
    if [[ "${VNX_AUTO_CLOSE:-0}" == "1" ]]; then
        cmd+=(--apply)
    fi
    "${cmd[@]}" >> "$log_file" 2>&1 || true
    echo "$now" > "$state_file"
}

# _maybe_learning_cycle — throttled daily learning cycle tick (D3).
# Invokes scripts/learning_loop.py run at most once per VNX_LEARNING_CYCLE_INTERVAL
# seconds when VNX_SUPERVISOR_MODE=unified AND VNX_LEARNING_ENABLED=1.
# Default (unset) = no behaviour change (off by default, operator opt-in).
# Logs to VNX_LOGS_DIR/learning_cycle.log.
_maybe_learning_cycle() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
    [[ "${VNX_LEARNING_ENABLED:-0}" == "1" ]] || return 0
    local interval="${VNX_LEARNING_CYCLE_INTERVAL:-86400}"
    local state_file="$STATE_DIR/.last_learning_cycle_ts"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if (( now - last < interval )); then
        return 0
    fi
    local log_file="$VNX_LOGS_DIR/learning_cycle.log"
    mkdir -p "$(dirname "$log_file")"
    python3 "$VNX_DIR/scripts/learning_loop.py" run >> "$log_file" 2>&1 || true
    echo "$now" > "$state_file"
}

# _unified_supervisor_lease_sweep_tick — throttled lease_sweep tick (SUP-PR2).
# Invokes scripts/lib/lease_sweep.py at most once per
# VNX_LEASE_SWEEP_INTERVAL_SEC seconds when VNX_SUPERVISOR_MODE=unified.
# Default (unset/legacy) = no behaviour change.
_unified_supervisor_lease_sweep_tick() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0

    local state_file="$VNX_DATA_DIR/state/.last_lease_sweep_ts"
    local interval="${VNX_LEASE_SWEEP_INTERVAL_SEC:-30}"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if (( now - last >= interval )); then
        mkdir -p "$VNX_LOGS_DIR" "$(dirname "$state_file")"
        python3 "$SCRIPT_DIR/lib/lease_sweep.py" \
            >> "$VNX_LOGS_DIR/lease_sweep.log" 2>&1 || true
        echo "$now" > "$state_file"
    fi
}
