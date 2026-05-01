#!/bin/bash
# dispatcher_supervisor.sh — Auto-restart supervisor for dispatcher_v8_minimal.sh
#
# Monitors dispatcher_v8_minimal.sh and restarts it after crashes.
# Uses exponential backoff (BACKOFF_INIT→BACKOFF_MAX) to avoid tight loops on
# persistent failures. Resets backoff after the dispatcher runs longer than
# BACKOFF_STABLE seconds. Enforces singleton so only one supervisor runs per
# VNX session.
#
# Usage:
#   bash scripts/dispatcher_supervisor.sh          # continuous loop
#   bash scripts/dispatcher_supervisor.sh --once   # start once, no restart (testing)
#   bash scripts/dispatcher_supervisor.sh status   # check if supervisor is running

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/process_lifecycle.sh"

VNX_DIR="$VNX_HOME"
DISPATCHER_SCRIPT="${VNX_DISPATCHER_SCRIPT:-$SCRIPT_DIR/dispatcher_v8_minimal.sh}"
SUPERVISOR_NAME="dispatcher_supervisor"
DISPATCHER_NAME="dispatcher_v8_minimal"

LOG_FILE="$VNX_LOGS_DIR/dispatcher_supervisor.log"
PID_FILE="$VNX_PIDS_DIR/${SUPERVISOR_NAME}.pid"
DISPATCHER_PID_FILE="$VNX_PIDS_DIR/${DISPATCHER_NAME}.pid"
DISPATCHER_LOCK_DIR="$VNX_LOCKS_DIR/${DISPATCHER_NAME}.lock"

# Backoff configuration (seconds)
BACKOFF_INIT="${VNX_SUPERVISOR_BACKOFF_INIT:-2}"
BACKOFF_MAX="${VNX_SUPERVISOR_BACKOFF_MAX:-60}"
BACKOFF_STABLE="${VNX_SUPERVISOR_BACKOFF_STABLE:-60}"

# Parse arguments
ONCE_MODE=0
for arg in "$@"; do
    case "$arg" in
        --once) ONCE_MODE=1 ;;
        status)
            if [ -f "$PID_FILE" ]; then
                pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
                if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                    echo "dispatcher_supervisor: running (PID: $pid)"
                    exit 0
                fi
            fi
            echo "dispatcher_supervisor: not running"
            exit 1
            ;;
    esac
done

# ---

mkdir -p "$(dirname "$LOG_FILE")" "$VNX_PIDS_DIR" "$VNX_LOCKS_DIR"
exec >> "$LOG_FILE" 2>&1

_log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# Singleton enforcement — save/restore SCRIPT_DIR since singleton_enforcer
# sources lib/process_lifecycle.sh which clobbers SCRIPT_DIR.
_SUP_SCRIPT_DIR="$SCRIPT_DIR"
source "$VNX_DIR/scripts/singleton_enforcer.sh"
SCRIPT_DIR="$_SUP_SCRIPT_DIR"
unset _SUP_SCRIPT_DIR
enforce_singleton "$SUPERVISOR_NAME" "$LOG_FILE" "$SCRIPT_DIR/dispatcher_supervisor.sh"
# singleton_enforcer sets EXIT+INT+TERM traps — override INT/TERM for child cleanup.

_DISPATCHER_PID=""
_STOP=0

_cleanup() {
    _STOP=1
    if [ -n "$_DISPATCHER_PID" ] && kill -0 "$_DISPATCHER_PID" 2>/dev/null; then
        _log "Stopping dispatcher child (PID: $_DISPATCHER_PID)..."
        kill -TERM "$_DISPATCHER_PID" 2>/dev/null || true
        local waited=0
        while kill -0 "$_DISPATCHER_PID" 2>/dev/null && [ "$waited" -lt 10 ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if kill -0 "$_DISPATCHER_PID" 2>/dev/null; then
            _log "Dispatcher did not stop in 10s — sending SIGKILL"
            kill -KILL "$_DISPATCHER_PID" 2>/dev/null || true
        fi
        _log "Dispatcher child stopped"
    fi
    rm -f "$PID_FILE"
    # EXIT trap from singleton_enforcer runs on exit and removes lock files.
    exit 0
}
trap '_cleanup' INT TERM

# Write supervisor PID file.
echo $$ > "$PID_FILE"

_clear_stale_dispatcher_lock() {
    local stale_pid=""

    if [ -f "$DISPATCHER_LOCK_DIR/pid" ]; then
        stale_pid=$(cat "$DISPATCHER_LOCK_DIR/pid" 2>/dev/null || echo "")
        if [ -n "$stale_pid" ] && ! kill -0 "$stale_pid" 2>/dev/null; then
            _log "Clearing stale dispatcher lock (dead PID: $stale_pid)"
            rm -rf "$DISPATCHER_LOCK_DIR"
        fi
    fi

    if [ -f "$DISPATCHER_PID_FILE" ]; then
        stale_pid=$(cat "$DISPATCHER_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$stale_pid" ] && ! kill -0 "$stale_pid" 2>/dev/null; then
            _log "Clearing stale dispatcher PID file (dead PID: $stale_pid)"
            rm -f "$DISPATCHER_PID_FILE" "${DISPATCHER_PID_FILE}.fingerprint"
        fi
    fi
}

_log "Dispatcher supervisor started (PID: $$)"
_log "Watching: $DISPATCHER_SCRIPT"
_log "Backoff config: init=${BACKOFF_INIT}s max=${BACKOFF_MAX}s stable=${BACKOFF_STABLE}s"

if [ ! -f "$DISPATCHER_SCRIPT" ]; then
    _log "FATAL: Dispatcher script not found: $DISPATCHER_SCRIPT"
    exit 1
fi

# Main restart loop
backoff=$BACKOFF_INIT
restart_count=0

while [ "$_STOP" = "0" ]; do
    _clear_stale_dispatcher_lock

    _log "Starting dispatcher (attempt #$((restart_count + 1)), backoff=${backoff}s on next crash)"
    start_ts=$(date +%s)

    bash "$DISPATCHER_SCRIPT" &
    _DISPATCHER_PID=$!
    _log "Dispatcher running (PID: $_DISPATCHER_PID)"

    set +e
    wait "$_DISPATCHER_PID"
    exit_code=$?
    set -e
    _DISPATCHER_PID=""

    # Trap may have set _STOP=1 and called exit; reaching here means natural exit.
    [ "$_STOP" = "1" ] && break

    end_ts=$(date +%s)
    runtime=$((end_ts - start_ts))
    _log "Dispatcher exited (rc=$exit_code, runtime=${runtime}s)"

    if [ "$ONCE_MODE" = "1" ]; then
        _log "Once mode — not restarting (rc=$exit_code)"
        rm -f "$PID_FILE"
        exit "$exit_code"
    fi

    # Reset backoff if the dispatcher ran long enough to be considered stable.
    if [ "$runtime" -ge "$BACKOFF_STABLE" ]; then
        _log "Dispatcher was stable (${runtime}s ≥ ${BACKOFF_STABLE}s) — resetting backoff"
        backoff=$BACKOFF_INIT
        restart_count=0
    fi

    restart_count=$((restart_count + 1))
    _log "Restarting dispatcher in ${backoff}s (restart #${restart_count})..."
    sleep "$backoff"

    # Exponential backoff, capped at max.
    backoff=$((backoff * 2))
    if [ "$backoff" -gt "$BACKOFF_MAX" ]; then
        backoff=$BACKOFF_MAX
    fi
done

rm -f "$PID_FILE"
_log "Dispatcher supervisor exiting"
