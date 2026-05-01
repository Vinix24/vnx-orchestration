#!/bin/bash
# receipt_processor_v4.sh - Time-aware receipt processing with flood protection
# Prevents reprocessing of historical reports and handles pane changes gracefully

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/receipt_terminal_detection.sh"

# Base directories
VNX_BASE="$VNX_HOME"
UNIFIED_REPORTS="$VNX_REPORTS_DIR"
HEADLESS_REPORTS="${VNX_HEADLESS_REPORTS_DIR:-$VNX_REPORTS_DIR/headless}"
STATE_DIR="$VNX_STATE_DIR"
SCRIPTS_DIR="$VNX_BASE/scripts"
APPEND_RECEIPT_SCRIPT="$SCRIPTS_DIR/append_receipt.py"

# PHASE 1C: Singleton enforcement - prevent duplicate processes
source "$SCRIPTS_DIR/singleton_enforcer.sh"
enforce_singleton "receipt_processor_v4.sh"

# Source the smart pane manager
source "$SCRIPTS_DIR/pane_manager_v2.sh"

# Configuration (can be overridden by environment variables)
MAX_AGE_HOURS="${VNX_MAX_AGE_HOURS:-24}"        # Only process reports from last N hours
RATE_LIMIT="${VNX_RATE_LIMIT:-10}"              # Max receipts per minute
FLOOD_THRESHOLD="${VNX_FLOOD_THRESHOLD:-50}"    # Circuit breaker threshold
MODE="${VNX_MODE:-monitor}"                     # monitor|catchup|manual
POLL_INTERVAL="${VNX_POLL_INTERVAL:-5}"          # seconds between report directory scans
# FSWATCH_RETRIES="${VNX_FSWATCH_RETRIES:-3}"   # (disabled) fswatch restart attempts before polling fallback
# FSWATCH_BACKOFF="${VNX_FSWATCH_BACKOFF:-5}"   # (disabled) seconds between fswatch restarts
CONFIRMATION_GRACE_SECONDS="${VNX_CONFIRMATION_GRACE_SECONDS:-300}"  # Lease window for no-confirmation blocks
FLOOD_LOCK_MAX_AGE="${VNX_FLOOD_LOCK_MAX_AGE:-300}"  # Auto-clear flood lock after N seconds (default 5 min)

# State files
LAST_PROCESSED="$STATE_DIR/receipt_last_processed"
PROCESSED_HASHES="$STATE_DIR/processed_receipts.txt"
PROCESSING_LOG="$STATE_DIR/receipt_processing.log"
FLOOD_LOCKFILE="$STATE_DIR/receipt_flood.lock"
RECEIPT_FILE="$STATE_DIR/t0_receipts.ndjson"
WATERMARK_FILE="$STATE_DIR/receipt_processor_watermark"
PID_FILE="$VNX_PIDS_DIR/receipt_processor.pid"

# Outbox directories for guaranteed receipt delivery (outbox pattern)
RECEIPTS_PENDING_DIR="${VNX_DATA_DIR}/receipts/pending"
RECEIPTS_PROCESSED_DIR="${VNX_DATA_DIR}/receipts/processed"
RECEIPT_RETRY_INTERVAL="${VNX_RECEIPT_RETRY_INTERVAL:-10}"  # seconds between pending retry sweeps

# Cross-platform SHA-256 helper (Linux: sha256sum, macOS: shasum -a 256)
_SHA256_FALLBACK_WARN=""
if command -v sha256sum >/dev/null 2>&1; then
    _sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    _sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    _sha256() { cksum "$1" | awk '{print $1}'; }
    _SHA256_FALLBACK_WARN="No sha256sum or shasum found; falling back to cksum (weaker)"
fi

# Create state files if they don't exist
touch "$PROCESSED_HASHES" "$RECEIPT_FILE" "$PROCESSING_LOG"

# Store PID
echo $$ > "$PID_FILE"

# ── Helper libraries ──────────────────────────────────────────────────────────
RP_LIB="$SCRIPT_DIR/lib/receipt_processor"
# shellcheck source=lib/receipt_processor/rp_logging.sh
source "$RP_LIB/rp_logging.sh"

# Emit deferred SHA fallback warning now that log() is defined
[ -n "$_SHA256_FALLBACK_WARN" ] && log "WARN" "$_SHA256_FALLBACK_WARN"

# shellcheck source=lib/receipt_processor/rp_time.sh
source "$RP_LIB/rp_time.sh"
# shellcheck source=lib/receipt_processor/rp_dedup.sh
source "$RP_LIB/rp_dedup.sh"
# shellcheck source=lib/receipt_processor/rp_lock.sh
source "$RP_LIB/rp_lock.sh"
# shellcheck source=lib/receipt_processor/rp_extract.sh
source "$RP_LIB/rp_extract.sh"
# shellcheck source=lib/receipt_processor/rp_state.sh
source "$RP_LIB/rp_state.sh"
# shellcheck source=lib/receipt_processor/rp_pattern.sh
source "$RP_LIB/rp_pattern.sh"
# shellcheck source=lib/receipt_processor/rp_append.sh
source "$RP_LIB/rp_append.sh"
# shellcheck source=lib/receipt_processor/rp_dispatch.sh
source "$RP_LIB/rp_dispatch.sh"
# shellcheck source=lib/receipt_processor/rp_delivery.sh
source "$RP_LIB/rp_delivery.sh"

# ─── Main orchestrator ───────────────────────────────────────────────────────

# Extract terminal from report name with metadata fallback.
_psr_extract_terminal() {
    local report_name="$1" report_path="$2"
    local terminal
    terminal="$(vnx_receipt_terminal_from_report_name "$report_name")"
    if [ -z "$terminal" ]; then
        local parsed_terminal
        parsed_terminal=$(python3 "$SCRIPTS_DIR/report_parser.py" "$report_path" 2>/dev/null | jq -r '.terminal // empty')
        if [ -n "$parsed_terminal" ]; then
            log "DEBUG" "Extracted terminal from metadata: $parsed_terminal"
            echo "$parsed_terminal"
        else
            log "WARN" "Could not determine terminal for: $report_name (skipping)"
            return 1
        fi
    else
        echo "$terminal"
    fi
}

# Parse report file into receipt JSON via report_parser.py.
_psr_parse_receipt_json() {
    local report_path="$1" report_name="$2"
    local receipt_json
    receipt_json=$(python3 "$SCRIPTS_DIR/report_parser.py" "$report_path" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$receipt_json" ]; then
        log_structured_failure "receipt_parse_failed" "report_parser.py failed to generate receipt JSON" "report=$report_name"
        log "ERROR" "Failed to parse report: $report_name"
        return 1
    fi
    echo "$receipt_json"
}

# Release canonical lease when event type warrants it (non-fatal).
# Skips no_confirmation timeouts: the terminal stays blocked to prevent immediate re-dispatch.
_psr_release_lease_on_event() {
    local event_type="$1" status="$2" terminal="$3" dispatch_id="$4"
    if [ "$event_type" = "task_complete" ] || [ "$event_type" = "task_failed" ] \
       || { [ "$event_type" = "task_timeout" ] && [ "$status" != "no_confirmation" ]; }; then
        _auto_release_lease_on_receipt "$terminal" "$dispatch_id"
    fi
}

# Update dispatch outcome in quality_intelligence.db and recompute CQS (non-fatal).
_psr_update_dispatch_outcome() {
    local dispatch_id="$1" event_type="$2" status="$3" report_path="$4" timestamp="$5"
    [ -n "$dispatch_id" ] || return 0
    case "$event_type" in task_complete|task_failed|task_timeout) ;; *) return 0 ;; esac
    python3 -c "
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path('$SCRIPTS_DIR') / 'lib'))
from vnx_paths import ensure_env
p = ensure_env()
db = Path(p['VNX_STATE_DIR']) / 'quality_intelligence.db'
if not db.exists(): sys.exit(0)
conn = sqlite3.connect(str(db))
conn.execute(
    'UPDATE dispatch_metadata SET outcome_status=?, outcome_report_path=?, completed_at=? WHERE dispatch_id=? AND outcome_status IS NULL',
    ('$3', '${4:-}', '$5', '$1')
)
conn.commit()
conn.close()
" 2>/dev/null || log "WARN" "Failed to update dispatch_metadata outcome (non-fatal)"
    python3 "$SCRIPTS_DIR/update_dispatch_cqs.py" --dispatch-id "$dispatch_id" 2>/dev/null \
        || log "WARN" "Failed to update dispatch CQS (non-fatal)"
}

# Verify dispatch contract claims on task_complete (non-fatal, Phase 2a).
_psr_verify_contract() {
    local dispatch_id="$1" event_type="$2"
    [ -n "$dispatch_id" ] && [ "$event_type" = "task_complete" ] || return 0
    local verify_result verify_rc verdict
    verify_result=$(python3 "$SCRIPTS_DIR/verify_claims.py" --dispatch-id "$dispatch_id" --store 2>/dev/null)
    verify_rc=$?
    if [ $verify_rc -eq 0 ]; then
        verdict=$(echo "$verify_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','unknown'))" 2>/dev/null)
        case "$verdict" in
            pass)        log "INFO"  "CONTRACT VERIFIED: dispatch=$dispatch_id verdict=pass" ;;
            no_contract) log "DEBUG" "No contract block in dispatch=$dispatch_id (Phase 2a: skip)" ;;
            *)           log "INFO"  "CONTRACT RESULT: dispatch=$dispatch_id verdict=$verdict" ;;
        esac
    elif [ $verify_rc -eq 1 ]; then
        log "WARN" "CONTRACT FAILED: dispatch=$dispatch_id — one or more claims failed"
    else
        log "WARN" "Contract verification error for dispatch=$dispatch_id (rc=$verify_rc, non-fatal)"
    fi
}

# Record pattern adoption signals for completed dispatches (non-fatal, A-5).
_psr_record_adoption() {
    local dispatch_id="$1" terminal="$2" report_path="$3" event_type="$4"
    [ -n "$dispatch_id" ] && [ "$event_type" = "task_complete" ] || return 0
    python3 "$SCRIPTS_DIR/gather_intelligence.py" record-adoption \
        "$dispatch_id" "${terminal:-unknown}" "$report_path" 2>/dev/null \
        || log "DEBUG" "Pattern adoption recording skipped (non-fatal)"
}

# Process a single report — orchestrates extracted sub-functions.
process_single_report() {
    local report_path="$1"
    local report_name=$(basename "$report_path")

    log "INFO" "Processing: $report_name"

    local terminal
    terminal="$(_psr_extract_terminal "$report_name" "$report_path")" || return 1

    local receipt_json
    receipt_json="$(_psr_parse_receipt_json "$report_path" "$report_name")" || return 1
    receipt_json=$(_hydrate_receipt_identity "$receipt_json" "$terminal")

    append_and_track_receipt "$report_path" "$report_name" "$receipt_json"
    local append_rc=$?
    [ $append_rc -eq 1 ] && return 1

    extract_receipt_fields "$receipt_json"

    if [ $append_rc -eq 2 ]; then
        log "INFO" "Skipping downstream processing for duplicate: $report_name"
        return 0
    fi

    update_receipt_shadow_state "$terminal"
    _move_dispatch_to_completed
    _psr_release_lease_on_event "$_rf_event_type" "$_rf_status" "$terminal" "$_rf_dispatch_id"
    _psr_update_dispatch_outcome "$_rf_dispatch_id" "$_rf_event_type" "$_rf_status" "$report_path" "$_rf_timestamp"
    attach_pr_evidence "$receipt_json" "$report_path"
    _psr_verify_contract "$_rf_dispatch_id" "$_rf_event_type"
    update_track_progress "$receipt_json" "$terminal"
    send_receipt_to_t0 "$receipt_json" "$terminal"

    python3 "$SCRIPTS_DIR/cost_tracker.py" 2>/dev/null \
        || log "WARN" "Failed to update cost metrics (non-fatal)"

    _psr_record_adoption "$_rf_dispatch_id" "$terminal" "$report_path" "$_rf_event_type"

    return 0
}

# Collect pending reports into _PPR_PENDING_REPORTS[] and _PPR_QUEUE_COUNT globals.
# Scans unified_reports/ only — headless/ gate reports are recorded separately via
# review_gates/results/ and produce ghost pings if scanned here.
_ppr_collect_pending() {
    _PPR_PENDING_REPORTS=()
    _PPR_QUEUE_COUNT=0
    for report in "$UNIFIED_REPORTS"/*.md; do
        [ -f "$report" ] || continue
        if should_process_report "$report"; then
            _PPR_PENDING_REPORTS+=("$report")
            ((_PPR_QUEUE_COUNT++))
        fi
    done
}

# Process reports (passed as "$@") with rate limiting and watermark update.
_ppr_process_rate_limited() {
    local processed_count=0
    local MAX_PROCESSED_MTIME=0
    for report in "$@"; do
        if [ "$processed_count" -ge "$RATE_LIMIT" ]; then
            log "INFO" "Rate limit reached ($RATE_LIMIT/min), pausing..."
            sleep 60
            processed_count=0
        fi
        if process_single_report "$report"; then
            ((processed_count++))
            local _mtime
            _mtime=$(stat -c %Y "$report" 2>/dev/null || stat -f %m "$report" 2>/dev/null)
            [ -n "$_mtime" ] && [ "$_mtime" -gt "$MAX_PROCESSED_MTIME" ] && MAX_PROCESSED_MTIME=$_mtime
        fi
        sleep 0.5
    done
    # Update watermark once with the highest mtime seen so non-chronological processing
    # order cannot cause older files to be skipped on the next sweep.
    [ "$MAX_PROCESSED_MTIME" -gt 0 ] && echo "$MAX_PROCESSED_MTIME" > "$WATERMARK_FILE"
    log "INFO" "Processed $processed_count reports successfully"
}

# Process all pending reports with flood protection and rate limiting.
process_pending_reports() {
    local cutoff=$(get_cutoff_time)
    log "INFO" "Scanning for reports newer than: $cutoff"
    _ppr_collect_pending
    if ! check_flood_protection "$_PPR_QUEUE_COUNT"; then
        log "ERROR" "Aborting due to flood protection"
        return 1
    fi
    if [ "$_PPR_QUEUE_COUNT" -eq 0 ]; then
        log "INFO" "No pending reports to process"
        return 0
    fi
    log "INFO" "Processing $_PPR_QUEUE_COUNT pending reports..."
    _ppr_process_rate_limited "${_PPR_PENDING_REPORTS[@]}"
}

# Monitor mode - watch for new reports only
# Polls the unified_reports directory at POLL_INTERVAL (default 5s).
# Previously used fswatch for sub-second detection, but the external process
# caused orphan fswatches, duplicate watchers, and fseventsd memory bloat
# (5+ GB observed). Polling at 5s is effectively free (<1ms per cycle) and
# eliminates the entire class of process-lifecycle bugs.
_poll_new_reports() {
    log "INFO" "Using polling mode (${POLL_INTERVAL}s intervals, receipt retry every ${RECEIPT_RETRY_INTERVAL}s)"
    local _retry_cycles=$(( RECEIPT_RETRY_INTERVAL / POLL_INTERVAL ))
    [ "$_retry_cycles" -lt 1 ] && _retry_cycles=1
    local _cycle=0
    while true; do
        local _poll_max_mtime=0
        for report in "$UNIFIED_REPORTS"/*.md "$HEADLESS_REPORTS"/*.md; do
            [ -f "$report" ] || continue
            if should_process_report "$report" && process_single_report "$report"; then
                local _mtime
                _mtime=$(stat -c %Y "$report" 2>/dev/null || stat -f %m "$report" 2>/dev/null)
                if [ -n "$_mtime" ] && [ "$_mtime" -gt "$_poll_max_mtime" ]; then
                    _poll_max_mtime=$_mtime
                fi
            fi
        done
        # Update watermark once after the full sweep with the maximum mtime seen.
        if [ "$_poll_max_mtime" -gt 0 ]; then
            echo "$_poll_max_mtime" > "$WATERMARK_FILE"
        fi
        _cycle=$(( _cycle + 1 ))
        if [ $(( _cycle % _retry_cycles )) -eq 0 ]; then
            _retry_pending_receipts
        fi
        sleep "$POLL_INTERVAL"
    done
}

# Process any reports from the last 10 minutes created while the processor was down.
_mnr_startup_catchup() {
    local catchup_count=0
    local now=$(date +%s)
    for report in "$UNIFIED_REPORTS"/*.md "$HEADLESS_REPORTS"/*.md; do
        [ -f "$report" ] || continue
        local mtime=$(stat -f%m "$report" 2>/dev/null || stat -c%Y "$report" 2>/dev/null || echo 0)
        local age_secs=$(( now - mtime ))
        if [ "$age_secs" -le 600 ] && should_process_report "$report"; then
            log "INFO" "Startup catchup: processing $( basename "$report" ) (age: ${age_secs}s)"
            process_single_report "$report" && ((catchup_count++))
        fi
    done
    [ "$catchup_count" -gt 0 ] && log "INFO" "Startup catchup complete: $catchup_count reports processed"
}

# ── fswatch (disabled) ────────────────────────────────────────────────────────
# Previously used fswatch for sub-second file detection. Disabled because:
#   - Orphan fswatch processes surviving parent death (PPID → 1)
#   - Duplicate watchers from singleton race under memory pressure
#   - fseventsd memory bloat (5+ GB observed with multiple watchers)
#   - macOS FSEvents silently dropping rapid create+close events
# Polling at 5s is effectively free and eliminates all of the above.
# To re-enable: set VNX_USE_FSWATCH=1; see git history for the original code block.
# ─────────────────────────────────────────────────────────────────────────────

# Watch for new reports via polling. On startup, performs a catchup scan and
# delivers any receipts pending since the last shutdown.
monitor_new_reports() {
    log "INFO" "Starting MONITOR mode - only new reports will be processed"
    date '+%Y%m%d-%H%M%S' > "$LAST_PROCESSED"
    _mnr_startup_catchup
    _retry_pending_receipts
    _poll_new_reports
}

# Cleanup on exit - gracefully stop child processes (fswatch, subshells) to prevent orphans.
# pkill -P sends SIGTERM (graceful), not SIGKILL — children get a chance to clean up.
cleanup() {
    log "INFO" "Shutting down receipt processor (PID: $$)..."
    pkill -TERM -P $$ 2>/dev/null || true
    sleep 0.5
    release_receipt_lock  # Ensure lock is released
    rm -f "$PID_FILE"
    rm -f "$FLOOD_LOCKFILE"  # Clear flood lock on clean shutdown
    # Clean up singleton lock (and legacy fswatch FIFO if it exists)
    rm -f "$STATE_DIR/.fswatch_fifo.$$"
    rm -rf "$VNX_LOCKS_DIR/receipt_processor_v4.sh.lock"
    rm -f "$VNX_PIDS_DIR/receipt_processor_v4.sh.pid" "$VNX_PIDS_DIR/receipt_processor_v4.sh.pid.fingerprint"
}

trap cleanup EXIT INT TERM

# Main execution
log "INFO" "Receipt Processor v4 starting (PID: $$)"
log "INFO" "Mode: $MODE | Max age: ${MAX_AGE_HOURS}h | Rate limit: ${RATE_LIMIT}/min"

# Check pane health first
if ! check_pane_health >/dev/null 2>&1; then
    log "WARN" "Some panes are not healthy, attempting setup..."
    setup_pane_titles
fi

case "$MODE" in
    monitor)
        monitor_new_reports
        ;;
    catchup)
        log "INFO" "CATCHUP mode - processing reports from last ${MAX_AGE_HOURS} hours"
        process_pending_reports
        log "INFO" "Catchup complete, switching to monitor mode"
        MODE="monitor"
        monitor_new_reports
        ;;
    manual)
        log "INFO" "MANUAL mode - processing pending reports once"
        process_pending_reports
        log "INFO" "Manual processing complete"
        ;;
    *)
        log "ERROR" "Invalid mode: $MODE (use: monitor|catchup|manual)"
        exit 1
        ;;
esac
