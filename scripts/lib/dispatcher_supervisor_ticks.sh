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
#   VNX_OI_BRIDGE_INTERVAL         — seconds between OI→track bridge runs; default 900 (D1)

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

# _maybe_oi_bridge_tick — throttled OI→track bridge tick (D1, oi-bridge-continuous).
# Invokes scripts/import_open_items_to_tracks.py at most once per
# VNX_OI_BRIDGE_INTERVAL seconds when VNX_SUPERVISOR_MODE=unified — the SAME
# gating condition as _maybe_objective_reconcile (no separate divergent flag).
# Called BEFORE _maybe_objective_reconcile in process_dispatches() so the
# reconciler's derived_status / close_track_if_done blocker-check reads
# freshly-synced track_open_items in the same tick. The bridge already dedupes
# on (project_id, oi_id) (R4.4), so a repeated run is idempotent — no duplicate
# track_open_items rows.
#
# Best-effort: a bridge failure never crashes the supervisor. It DOES persist a
# freshness signal at $STATE_DIR/.oi_bridge_fresh ("1" iff the run's exit code
# means track_open_items committed successfully — 0 clean, or 4 which is only a
# post-commit ledger-emit warning per import_open_items_to_tracks.py's own exit
# contract; any other exit code, or the python3 invocation failing outright,
# writes "0"). _maybe_objective_reconcile reads this file before adding --apply
# so auto-close never trusts a track_open_items snapshot a failed bridge run
# left stale (safety-critical — see claudedocs/plan-oi-bridge-continuous.md).
# Default (unset/legacy VNX_SUPERVISOR_MODE) = no behaviour change.
_maybe_oi_bridge_tick() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
    local interval="${VNX_OI_BRIDGE_INTERVAL:-900}"
    local state_file="$STATE_DIR/.last_oi_bridge_ts"
    local fresh_file="$STATE_DIR/.oi_bridge_fresh"
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
    local log_file="$VNX_LOGS_DIR/oi_bridge.log"
    mkdir -p "$(dirname "$log_file")"
    local rc=0
    python3 "$VNX_DIR/scripts/import_open_items_to_tracks.py" \
        --project-id "$VNX_PROJECT_ID" \
        --state-dir "$STATE_DIR" \
        >> "$log_file" 2>&1 || rc=$?
    if [[ "$rc" == "0" || "$rc" == "4" ]]; then
        echo "1" > "$fresh_file"
    else
        echo "0" > "$fresh_file"
        log "V-OI-BRIDGE WARN: OI bridge tick failed (exit $rc); see $log_file — reconcile-apply is gated this cycle until the next successful bridge run"
    fi
    echo "$now" > "$state_file"
}

# _maybe_objective_reconcile — throttled git-grounded reconcile tick (D4).
# Invokes `objective reconcile` at most once per VNX_OBJECTIVE_RECONCILE_INTERVAL seconds
# when VNX_SUPERVISOR_MODE=unified. Auto-close is ON BY DEFAULT (operator directive
# 2026-07-10, matching the SessionStart hook): --apply is added unless the operator opts
# out with VNX_AUTO_CLOSE=0 → advisory CHECK (zero writes). reconcile only closes tracks
# whose linked PRs are verified MERGED and whose blockers are resolved, so applying by
# default keeps the horizon in sync with git reality; the streak is still computed for
# observability but no longer gates the flip. Best-effort (|| true); logged to
# objective_reconcile.log. Default (unset/legacy VNX_SUPERVISOR_MODE) = no behaviour change.
#
# D1 bridge-freshness gate: --apply is additionally conditioned on
# $STATE_DIR/.oi_bridge_fresh == "1" (written by _maybe_oi_bridge_tick, which
# runs earlier in the same process_dispatches() cycle). A missing/failed/
# unattempted bridge signal fails CLOSED — reconcile still runs (advisory
# derived_status refresh + summary/history), it just withholds --apply for
# this cycle rather than auto-closing against a track_open_items snapshot that
# may not reflect the current open-items store.
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
    if [[ "${VNX_AUTO_CLOSE:-1}" != "0" ]]; then
        local oi_bridge_fresh="0"
        local fresh_file="$STATE_DIR/.oi_bridge_fresh"
        if [[ -f "$fresh_file" ]]; then
            oi_bridge_fresh=$(cat "$fresh_file" 2>/dev/null || echo 0)
        fi
        if [[ "$oi_bridge_fresh" == "1" ]]; then
            cmd+=(--apply)
        else
            log "V-OI-BRIDGE WARN: skipping --apply for this objective-reconcile tick — OI bridge freshness signal is '$oi_bridge_fresh' (expected '1'); auto-close withheld until the next successful bridge run"
        fi
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
