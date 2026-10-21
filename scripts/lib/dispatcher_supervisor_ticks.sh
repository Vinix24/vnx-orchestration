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
#   VNX_COST_LEDGER_REPLAY_INTERVAL_SEC — seconds between cost-ledger spool replays; default 60.
#                                    Not gated on VNX_SUPERVISOR_MODE (ADR-043 §6).
#   VNX_COST_LEDGER_TICK_TIMEOUT_SEC — the replay's hard timeout; default and maximum 20

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
    printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
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
    # Exit 4 is import_open_items_to_tracks.py's own exit contract for "the
    # track_open_items commit succeeded, but the post-commit ledger-emit step
    # only warned" — the write already landed, so freshness still holds. Any
    # other non-zero exit means the commit itself is suspect and freshness
    # must fail closed.
    local -r OI_BRIDGE_RC_LEDGER_WARN=4
    if [[ "$rc" == "0" || "$rc" == "$OI_BRIDGE_RC_LEDGER_WARN" ]]; then
        printf '%s' "1" > "$fresh_file.tmp.$$" && mv -f "$fresh_file.tmp.$$" "$fresh_file"
        log "V-OI-BRIDGE INFO: OI bridge tick succeeded (exit $rc); freshness=1 written to $fresh_file"
    else
        printf '%s' "0" > "$fresh_file.tmp.$$" && mv -f "$fresh_file.tmp.$$" "$fresh_file"
        log "V-OI-BRIDGE WARN: OI bridge tick failed (exit $rc); see $log_file — reconcile-apply is gated this cycle until the next successful bridge run"
    fi
    # OI-798: atomic tmp+rename for the timestamp marker too — same pattern as
    # the freshness write above. A truncating echo> leaves a torn marker if the
    # supervisor dies mid-write, which would delay the next bridge tick by a
    # full interval (the throttle reads this file on every tick).
    printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
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
    printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
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
    printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
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
        printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
    fi
}

# The hard timeout of the cost-ledger replay tick, in whole seconds: 20, or a
# lower VNX_COST_LEDGER_TICK_TIMEOUT_SEC (1-20). A higher or malformed value
# gives 20: the dispatcher never waits longer than that on the spool.
_cost_ledger_tick_timeout() {
    local limit="${VNX_COST_LEDGER_TICK_TIMEOUT_SEC:-20}"
    if [[ ! "$limit" =~ ^[0-9]+$ ]] || (( 10#$limit < 1 || 10#$limit > 20 )); then
        limit=20
    fi
    printf '%s' "$(( 10#$limit ))"
}

# _run_killed_after <seconds> <out_file> <command...> — run the command with
# stdout and stderr appended to out_file, and SIGKILL it once <seconds> pass.
# Returns its exit status, 137 when it was killed. A watchdog sleeps beside
# it and is stopped (its sleep included) as soon as the command ends; bash
# 3.2 (macOS /bin/bash) has no `wait -n`, so the watchdog does the killing.
_run_killed_after() {
    local limit="$1" out="$2"
    shift 2
    "$@" >> "$out" 2>&1 < /dev/null &
    local pid=$!
    (
        local timer=""
        trap '[[ -n "$timer" ]] && kill "$timer" 2>/dev/null; exit 0' TERM
        sleep "$limit" &
        timer=$!
        wait "$timer" || { kill "$timer" 2>/dev/null; exit 0; }
        kill -KILL "$pid" 2>/dev/null
    ) &
    local watchdog=$!
    local rc=0
    # 2>/dev/null: no "Killed: 9" job notice from bash in the dispatcher's output; the caller logs the kill.
    wait "$pid" 2>/dev/null || rc=$?
    kill -TERM "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
    return "$rc"
}

# _maybe_cost_ledger_replay — the cost ledger's spool replay tick (ADR-043 §6, K2a-3c).
# Runs scripts/cost_ledger_replay.py at most once per
# VNX_COST_LEDGER_REPLAY_INTERVAL_SEC seconds (default 60), in EVERY supervisor
# mode: the spool is the ledger's recovery path, not supervision, so this tick
# does not look at VNX_SUPERVISOR_MODE. The replay is a subprocess with a hard
# timeout of 20 seconds (_cost_ledger_tick_timeout) and is killed after it; it
# bounds itself to 10 seconds (VNX_COST_LEDGER_REPLAY_BUDGET_SEC), so the kill
# is the backstop for a replay that hangs. After the replay the script writes
# the cost-ledger beacon. With an empty spool it writes nothing.
#
# Every failure is logged (a non-zero exit, a kill, a log or threshold file
# that cannot be written) and the tick ALWAYS returns 0: a failing or hanging
# replay never stops the dispatcher and never holds it longer than the
# timeout. The threshold is kept in $STATE_DIR/.last_cost_ledger_replay_ts and,
# for this dispatcher process, in _COST_LEDGER_REPLAY_LAST, so a threshold file
# that cannot be written still throttles the tick.
_COST_LEDGER_REPLAY_LAST=0
_maybe_cost_ledger_replay() {
    local interval="${VNX_COST_LEDGER_REPLAY_INTERVAL_SEC:-60}"
    [[ "$interval" =~ ^[0-9]+$ ]] || interval=60
    local state_file="$STATE_DIR/.last_cost_ledger_replay_ts"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    (( _COST_LEDGER_REPLAY_LAST > last )) && last=$_COST_LEDGER_REPLAY_LAST
    if (( now - last < 10#$interval )); then
        return 0
    fi
    _COST_LEDGER_REPLAY_LAST=$now

    local log_file="$VNX_LOGS_DIR/cost_ledger_replay.log"
    if ! { mkdir -p "$VNX_LOGS_DIR" && : >> "$log_file"; } 2>/dev/null; then
        log "V-COST-LEDGER WARN: the replay log $log_file cannot be written; the replay runs without it"
        log_file=/dev/null
    fi
    local limit rc=0
    limit=$(_cost_ledger_tick_timeout)
    _run_killed_after "$limit" "$log_file" python3 "$VNX_DIR/scripts/cost_ledger_replay.py" || rc=$?
    if [[ "$rc" == "137" ]]; then
        log "V-COST-LEDGER WARN: the spool replay was killed after its hard timeout of ${limit}s; see $log_file"
    elif [[ "$rc" != "0" ]]; then
        log "V-COST-LEDGER WARN: the spool replay exited $rc (1: unreadable spool, write failure or mismatch; 3: lock busy); see $log_file"
    fi

    if ! { printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"; } 2>/dev/null; then
        rm -f "$state_file.tmp.$$" 2>/dev/null || true
        log "V-COST-LEDGER WARN: the threshold file $state_file cannot be written; this dispatcher throttles the tick in memory"
    fi
    return 0
}
