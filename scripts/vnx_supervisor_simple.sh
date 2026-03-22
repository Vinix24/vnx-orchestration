#!/bin/bash
# VNX Process Supervisor - Simple Version for Compatibility
# Bulletproof process management with flock and kill -0

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
# shellcheck source=lib/process_lifecycle.sh
source "$SCRIPT_DIR/lib/process_lifecycle.sh"
VNX_DIR="$VNX_HOME"
SCRIPTS_DIR="$VNX_DIR/scripts"
LOCK_DIR="$VNX_LOCKS_DIR"
PID_DIR="$VNX_PIDS_DIR"
LOG_DIR="$VNX_LOGS_DIR"
STATE_DIR="$VNX_STATE_DIR"

# Create required directories
mkdir -p "$LOCK_DIR" "$PID_DIR" "$LOG_DIR" "$STATE_DIR"

# Pane resolution (VNX_HYBRID_FINAL.sh writes VNX_STATE_DIR/panes.json each launch)
source "$SCRIPTS_DIR/pane_config.sh" 2>/dev/null || true

get_t0_pane() {
    if declare -F get_pane_id >/dev/null 2>&1; then
        get_pane_id "T0" "$STATE_DIR/panes.json"
        return 0
    fi

    if command -v jq >/dev/null 2>&1; then
        jq -r '.t0.pane_id // .T0.pane_id // "%0"' "$STATE_DIR/panes.json" 2>/dev/null || echo "%0"
        return 0
    fi

    echo "%0"
}

# Receipt processing - ALWAYS use receipt_processor_v4
RECEIPT_SERVICE_NAME="receipt_processor"
RECEIPT_SCRIPT="receipt_processor_v4.sh"

# Supervisor configuration
# SUPERVISOR_LOCK no longer needed - using mkdir for locking on macOS
SUPERVISOR_PID="$PID_DIR/vnx_supervisor.pid"
SUPERVISOR_LOG="$LOG_DIR/vnx_supervisor.log"
CHECK_INTERVAL=5  # seconds between health checks (reduced for faster recovery)
MAX_RESTART_ATTEMPTS=3  # Maximum restart attempts before alerting
RESTART_COOLDOWN=30  # Seconds to wait after MAX_RESTART_ATTEMPTS before trying again

# CRITICAL: Enforce singleton to prevent duplicate supervisor instances
source "$SCRIPTS_DIR/singleton_enforcer.sh"
enforce_singleton "vnx_supervisor" "$SUPERVISOR_LOG" "$SCRIPT_DIR/vnx_supervisor_simple.sh"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$SUPERVISOR_LOG" >&2
}

is_process_running() {
    local name="$1"
    local pid_file="$PID_DIR/${name}.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0  # Running
        else
            rm -f "$pid_file"
            return 1  # Not running
        fi
    fi
    return 1  # Not running
}

kill_duplicates() {
    local name="$1"
    local script="$2"
    local current_pid="$3"
    local fingerprint
    fingerprint="$(vnx_proc_realpath "$SCRIPTS_DIR/$script")"

    if vnx_proc_stop_duplicates "$name" "$fingerprint" "$current_pid" "$SUPERVISOR_LOG" "duplicate_cleanup" 5; then
        log "Stopped duplicate instances of $script"
        sleep 1
    fi
}

start_process() {
    local name="$1"
    local script="$2"
    local reason="${3:-supervisor_start}"
    local script_path="$SCRIPTS_DIR/$script"
    local pid_file="$PID_DIR/${name}.pid"
    local log_file="$LOG_DIR/${name}.log"
    
    # Check if already running
    if is_process_running "$name"; then
        local pid=$(cat "$pid_file")
        echo -e "${YELLOW}$name already running (PID: $pid)${NC}"
        return 0
    fi
    
    # Kill any duplicates first
    kill_duplicates "$name" "$script" "none"
    
    # Check if script exists
    if [ ! -f "$script_path" ]; then
        echo -e "${RED}Script not found: $script_path${NC}"
        return 1
    fi
    
    # Make script executable
    chmod +x "$script_path"
    
    echo "Starting $name..."
    
    # Note: Removed cleanup of /tmp PID files to respect process singleton management
    # Processes manage their own PID files
    
    # Start the process
    cd "$SCRIPTS_DIR"
    # Check if it's a Python script
    if [[ "$script" == *.py ]]; then
        if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
            source "$PROJECT_ROOT/.venv/bin/activate"
        fi
        nohup python "$script_path" >> "$log_file" 2>&1 &
    else
        nohup bash "$script_path" >> "$log_file" 2>&1 &
    fi
    local pid=$!

    # Write PID
    vnx_proc_write_pidfile "$pid_file" "$pid" "$(vnx_proc_realpath "$script_path")"

    # Extended verification with health check
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        echo -e "${RED}✗ $name failed to start (crashed immediately)${NC}"
        log "ERROR: $name crashed immediately after start (PID: $pid)"
        rm -f "$pid_file"
        return 1
    fi

    # Secondary health check after 3 more seconds
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
        # Final verification: check for actual script process, not just shell
        local script_running=$(pgrep -f "$script" 2>/dev/null | grep "$pid")
        if [ -n "$script_running" ]; then
            echo -e "${GREEN}✓ $name started (PID: $pid)${NC}"
            log "$name started and verified healthy (PID: $pid)"
            vnx_proc_log_event "$SUPERVISOR_LOG" "$name" "start" "$pid" "$reason"
            return 0
        else
            echo -e "${YELLOW}⚠ $name PID exists but script not found${NC}"
            log "WARNING: $name PID $pid exists but script process not found"
            return 0  # Return success anyway, monitor will catch if it fails
        fi
    else
        echo -e "${RED}✗ $name failed health check (crashed after 5s)${NC}"
        log "ERROR: $name crashed during health check (PID: $pid)"
        rm -f "$pid_file"
        return 1
    fi
}

stop_process() {
    local name="$1"
    local script="$2"
    local pid_file="$PID_DIR/${name}.pid"
    
    if ! is_process_running "$name"; then
        echo "$name is not running"
        return 0
    fi
    
    local pid=$(cat "$pid_file")
    echo "Stopping $name (PID: $pid)..."

    if ! vnx_proc_stop_pidfile "$name" "$pid_file" "$SUPERVISOR_LOG" "manual_stop" 5; then
        echo "Failed to stop $name safely"
    fi

    # Kill any remaining duplicates (safe ownership validation)
    kill_duplicates "$name" "$script" "none"
    
    echo -e "${GREEN}✓ $name stopped${NC}"
    log "$name stopped"
}

start_all() {
    log "Starting all VNX processes..."

    start_process "dispatcher" "dispatcher_v8_minimal.sh"
    # Note: Deprecated scripts moved to archive/legacy_scripts_2026-02-11/scripts/archived_phase1b/
    # - dispatcher_v7_compilation.sh (Phase 1B: replaced by V8 native skills)
    # - dispatcher_v6_ack_timeout.sh replaced by v7
    # - smart_tap_with_editor_multi.sh replaced by v7
    # - unified_state_manager.py replaced by v2
    start_process "smart_tap" "smart_tap_v7_json_translator.sh"
    start_process "$RECEIPT_SERVICE_NAME" "$RECEIPT_SCRIPT"
    # receipt_notifier.sh removed - replaced by receipt_processor_v4.sh (Phase 1B/1C)
    # ack_dispatcher_v2 is deprecated - dispatcher_v8_minimal handles all dispatching
    # dispatch_ack_watcher is also deprecated - heartbeat_ack_monitor socket path is canonical ACK flow
    # start_process "ack_dispatcher" "ack_dispatcher_v2.sh"
    start_process "heartbeat_ack_monitor" "heartbeat_ack_monitor.py"
    if [ "${VNX_QUEUE_POPUP_ENABLED:-1}" != "0" ]; then
      start_process "queue_watcher" "queue_popup_watcher.sh"
    else
      log "Queue popup disabled — starting auto-accept watcher instead"
      start_process "queue_watcher" "queue_auto_accept.sh"
    fi
    start_process "dashboard" "generate_valid_dashboard.sh"
    start_process "state_manager" "unified_state_manager_v2.py"
    start_process "intelligence_daemon" "intelligence_daemon.py"
    start_process "recommendations_engine" "recommendations_engine_daemon.sh"
    # report_watcher and receipt_notifier removed - receipt_processor_v4.sh handles
    # watch + parse + append + T0 notification as single process (dedup fix)

    log "All processes started"
}

stop_all() {
    log "Stopping all VNX processes..."

    stop_process "dispatcher" "dispatcher_v8_minimal.sh"
    # Using V8 - native skills with instruction-only dispatch (Phase 1B)
    stop_process "smart_tap" "smart_tap_v7_json_translator.sh"
    stop_process "$RECEIPT_SERVICE_NAME" "$RECEIPT_SCRIPT"
    # receipt_notifier.sh removed - replaced by receipt_processor_v4.sh (Phase 1B/1C)
    # ack_dispatcher_v2 is deprecated
    # stop_process "ack_dispatcher" "ack_dispatcher_v2.sh"
    stop_process "dispatch_ack_watcher" "dispatch_ack_watcher.sh"
    stop_process "heartbeat_ack_monitor" "heartbeat_ack_monitor.py"
    stop_process "queue_watcher" "queue_popup_watcher.sh"
    stop_process "dashboard" "generate_valid_dashboard.sh"
    stop_process "state_manager" "unified_state_manager_v2.py"
    stop_process "intelligence_daemon" "intelligence_daemon.py"
    stop_process "recommendations_engine" "recommendations_engine_daemon.sh"
    # report_watcher and receipt_notifier removed (handled by receipt_processor_v4)

    log "All processes stopped"
}

status() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}            VNX Process Status Report                   ${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}\n"

    for name in dispatcher smart_tap "$RECEIPT_SERVICE_NAME" heartbeat_ack_monitor queue_watcher dashboard state_manager intelligence_daemon recommendations_engine; do
        printf "%-25s: " "$name"
        if is_process_running "$name"; then
            local pid=$(cat "$PID_DIR/${name}.pid")
            echo -e "${GREEN}✅ Running (PID: $pid)${NC}"
        else
            echo -e "${RED}❌ NOT RUNNING${NC}"
        fi
    done
    
    echo ""
    
    # Check for duplicates
    echo "Checking for duplicate processes..."
    local has_duplicates=false
    for script in dispatcher_v8_minimal smart_tap receipt_processor queue_popup generate_valid_dashboard unified_state_manager report_watcher recommendations_engine_daemon; do
        local count=$(pgrep -fc "$script" 2>/dev/null || echo "0")
        if [ "$count" -gt 1 ]; then
            echo -e "${RED}⚠️  WARNING: $script has $count instances running!${NC}"
            has_duplicates=true
        fi
    done
    
    if [ "$has_duplicates" = false ]; then
        echo -e "${GREEN}✓ No duplicate processes detected${NC}"
    fi
    
    echo -e "\n${BLUE}═══════════════════════════════════════════════════════${NC}\n"
    
    # Dashboard info
    echo -e "${CYAN}Dashboard URL:${NC} http://localhost:8080"
    echo -e "${CYAN}Queue UI:${NC} Press Ctrl+G in tmux or run:"
    echo "  tmux display-popup -E '$SCRIPTS_DIR/queue_ui_enhanced.sh'"
}

monitor() {
    # Ensure singleton using mkdir (atomic on all UNIX systems, works on macOS)
    LOCK_DIR="$STATE_DIR/supervisor.lock"

    # Try to acquire lock
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        # Check if lock is stale (process dead)
        if [ -f "$LOCK_DIR/pid" ]; then
            OLD_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "0")
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                # Stale lock, remove it
                log "Removing stale lock from PID $OLD_PID"
                rm -rf "$LOCK_DIR"
                mkdir "$LOCK_DIR" 2>/dev/null || {
                    echo -e "${RED}Another supervisor instance is already running${NC}"
                    exit 1
                }
            else
                echo -e "${RED}Another supervisor instance is already running (PID: $OLD_PID)${NC}"
                exit 1
            fi
        else
            echo -e "${RED}Lock exists but no PID file - another instance may be starting${NC}"
            exit 1
        fi
    fi

    # Store PID in lock directory
    echo $$ > "$LOCK_DIR/pid"
    echo $$ > "$SUPERVISOR_PID"

    # Clean up lock on exit
    trap "rm -rf $LOCK_DIR; rm -f $SUPERVISOR_PID" EXIT

    # Restart tracking (file-based for bash 3.x compatibility)
    local restart_dir="$STATE_DIR/restart_tracking"
    mkdir -p "$restart_dir"

    log "VNX Supervisor started (PID: $$)"
    log "Starting continuous monitoring (checking every ${CHECK_INTERVAL}s)..."
    log "Auto-restart enabled for all supervised processes (all critical)"

    local rotation_cycle=0
    local ROTATION_INTERVAL=60  # Check log rotation every 60 cycles (5min at 5s interval)

    while true; do
        # Periodic log rotation check (every ~5 minutes)
        rotation_cycle=$((rotation_cycle + 1))
        if [ $rotation_cycle -ge $ROTATION_INTERVAL ]; then
            rotation_cycle=0
            log "Running periodic log rotation check..."
            bash "$SCRIPTS_DIR/daily_log_rotation.sh" >> "$SUPERVISOR_LOG" 2>&1 || \
                log "WARNING: Log rotation check failed"
        fi
        receipt_item="${RECEIPT_SERVICE_NAME}:${RECEIPT_SCRIPT}"
        # Build process list (conditionally include queue_watcher)
        local _monitor_items="dispatcher:dispatcher_v8_minimal.sh smart_tap:smart_tap_v7_json_translator.sh $receipt_item heartbeat_ack_monitor:heartbeat_ack_monitor.py"
        if [ "${VNX_QUEUE_POPUP_ENABLED:-1}" != "0" ]; then
            _monitor_items="$_monitor_items queue_watcher:queue_popup_watcher.sh"
        fi
        _monitor_items="$_monitor_items dashboard:generate_valid_dashboard.sh state_manager:unified_state_manager_v2.py intelligence_daemon:intelligence_daemon.py recommendations_engine:recommendations_engine_daemon.sh"
        # Check each process
        for item in $_monitor_items; do

            IFS=':' read -r name script <<< "$item"

            if ! is_process_running "$name"; then
                # Get restart tracking info (count|last_restart)
                local tracking_file="$restart_dir/${name}.txt"
                local restart_count=0
                local last_restart=0
                if [ -f "$tracking_file" ]; then
                    IFS='|' read -r restart_count last_restart < "$tracking_file"
                    restart_count="${restart_count:-0}"
                    last_restart="${last_restart:-0}"
                fi
                local current_time=$(date +%s)

                # Reset counter if cooldown period has passed
                if [ $((current_time - last_restart)) -gt $RESTART_COOLDOWN ]; then
                    restart_count=0
                fi

                # Check if we should attempt restart
                if [ $restart_count -lt $MAX_RESTART_ATTEMPTS ]; then
                    restart_count=$((restart_count + 1))
                    log "⚠️  CRITICAL: $name crashed (attempt $restart_count/$MAX_RESTART_ATTEMPTS) - restarting immediately"

                    if start_process "$name" "$script" "crash_restart"; then
                        log "✅ $name successfully restarted"
                        local restarted_pid=""
                        restarted_pid=$(cat "$PID_DIR/${name}.pid" 2>/dev/null || echo "")
                        if [ -n "$restarted_pid" ]; then
                            vnx_proc_log_event "$SUPERVISOR_LOG" "$name" "restart" "$restarted_pid" "crash_restart"
                        fi
                        echo "$restart_count|$current_time" > "$tracking_file"
                    else
                        log "❌ $name restart FAILED - will retry in ${CHECK_INTERVAL}s"
                        echo "$restart_count|$current_time" > "$tracking_file"
                    fi

                    # Alert if receipt processing crashed
                    if [ "$name" = "$RECEIPT_SERVICE_NAME" ]; then
                        log "🚨 Receipt processing system restarted - checking for missed receipts"
                    fi
                else
                    log "🚨 ALERT: $name has crashed $MAX_RESTART_ATTEMPTS times - waiting ${RESTART_COOLDOWN}s before retry"
                fi
            else
                # Process is running - reset restart counter
                local tracking_file="$restart_dir/${name}.txt"
                [ -f "$tracking_file" ] && rm -f "$tracking_file"
            fi
        done

        sleep "$CHECK_INTERVAL"
    done
}

case "${1:-}" in
    start)
        start_all
        monitor
        ;;
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 2
        start_all
        monitor
        ;;
    status)
        status
        ;;
    monitor)
        monitor
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|monitor}"
        echo ""
        echo "  start   - Start all processes and monitor"
        echo "  stop    - Stop all processes"
        echo "  restart - Restart all processes"
        echo "  status  - Show current status"
        echo "  monitor - Monitor and restart crashed processes"
        exit 1
        ;;
esac
