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

# Logging with levels
log() {
    local level="${1:-INFO}"
    shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" | tee -a "$PROCESSING_LOG" >&2
}

# Emit deferred SHA fallback warning now that log() is defined
[ -n "$_SHA256_FALLBACK_WARN" ] && log "WARN" "$_SHA256_FALLBACK_WARN"

log_structured_failure() {
    local code="$1"
    local message="$2"
    local details="${3:-}"
    local payload
    payload="$(python3 - "$code" "$message" "$details" <<'PY'
import json
import sys

code, message, details = sys.argv[1], sys.argv[2], sys.argv[3]
event = {
    "event": "failure",
    "component": "receipt_processor_v4.sh",
    "code": code,
    "message": message,
}
if details:
    event["details"] = details
print(json.dumps(event, separators=(",", ":")))
PY
)"
    log "ERROR" "$payload"
}

# Shadow write helper for terminal_state.json (non-fatal).
shadow_update_terminal_state() {
    local terminal_id="$1"
    local status="$2"
    local dispatch_id="${3:-}"
    local ts="${4:-}"
    local clear_claim="${5:-false}"
    local lease_seconds="${6:-}"

    local cmd=(
        python3 "$SCRIPTS_DIR/terminal_state_shadow.py"
        --terminal-id "$terminal_id"
        --status "$status"
        --last-activity "${ts:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    )

    if [ -n "$dispatch_id" ] && [ "$clear_claim" != "true" ]; then
        cmd+=(--claimed-by "$dispatch_id")
    fi

    if [ "$clear_claim" = "true" ]; then
        cmd+=(--clear-claim)
    fi
    if [ -n "$lease_seconds" ]; then
        cmd+=(--lease-seconds "$lease_seconds")
    fi

    if ! "${cmd[@]}" >/dev/null 2>&1; then
        log "WARN" "SHADOW: Failed terminal_state update (terminal=$terminal_id, status=$status)"
    fi
}

# Calculate cutoff timestamp based on mode
get_cutoff_time() {
    case "$MODE" in
        monitor)
            # Monitor mode: only process new reports from now on
            date '+%Y%m%d-%H%M%S'
            ;;
        catchup)
            # Catchup mode: process reports from last N hours
            # Cross-platform: try GNU date first, then BSD (macOS)
            date -d "-${MAX_AGE_HOURS} hours" '+%Y%m%d-%H%M%S' 2>/dev/null \
                || date -v-${MAX_AGE_HOURS}H '+%Y%m%d-%H%M%S'
            ;;
        manual)
            # Manual mode: use stored timestamp or default to 1 hour
            if [ -f "$LAST_PROCESSED" ]; then
                cat "$LAST_PROCESSED"
            else
                date -d "-1 hour" '+%Y%m%d-%H%M%S' 2>/dev/null \
                    || date -v-1H '+%Y%m%d-%H%M%S'
            fi
            ;;
    esac
}

# Extract timestamp from report filename
extract_timestamp() {
    local filename=$(basename "$1")
    # Match YYYYMMDD-HHMMSS pattern at start of filename
    echo "$filename" | grep -oE '^[0-9]{8}-[0-9]{6}'
}

# Check if report should be processed
should_process_report() {
    local report_file="$1"
    local report_name=$(basename "$report_file")

    # PERMANENT FIX: Use file modification time, NOT filename timestamp
    # This prevents reports with old dispatch timestamps from being rejected
    # when they are actually created NOW

    # Get file modification time in seconds since epoch (cross-platform)
    local file_mtime
    file_mtime=$(stat -c %Y "$report_file" 2>/dev/null || stat -f %m "$report_file" 2>/dev/null)
    if [ -z "$file_mtime" ]; then
        log "ERROR" "Cannot get modification time for: $report_name"
        return 1
    fi

    # Calculate cutoff time in seconds since epoch
    local cutoff_seconds
    if [ "$MODE" = "monitor" ]; then
        # Monitor mode: use watermark-based processing.
        # Only process reports newer than the last successfully processed report's mtime.
        # On first run (no watermark), fall back to 24 hours to avoid replaying history.
        if [ -f "$WATERMARK_FILE" ]; then
            cutoff_seconds=$(cat "$WATERMARK_FILE" 2>/dev/null)
            if ! [[ "$cutoff_seconds" =~ ^[0-9]+$ ]]; then
                cutoff_seconds=$(($(date +%s) - 86400))
            fi
        else
            cutoff_seconds=$(($(date +%s) - 86400))
        fi
    elif [ "$MODE" = "manual" ] && [ -f "$WATERMARK_FILE" ]; then
        # Manual mode: honor last-processed watermark (epoch seconds) when available
        cutoff_seconds=$(cat "$WATERMARK_FILE" 2>/dev/null)
        if ! [[ "$cutoff_seconds" =~ ^[0-9]+$ ]]; then
            cutoff_seconds=$(($(date +%s) - (MAX_AGE_HOURS * 3600)))
        fi
    else
        # Catchup mode (or manual with no prior watermark): process last N hours
        cutoff_seconds=$(($(date +%s) - (MAX_AGE_HOURS * 3600)))
    fi

    # Check if file is too old based on actual modification time
    if [ "$file_mtime" -lt "$cutoff_seconds" ]; then
        local age_minutes=$(( ($(date +%s) - file_mtime) / 60 ))
        log "DEBUG" "Report too old: $report_name (age: ${age_minutes}m)"
        return 1
    fi

    log "DEBUG" "Report accepted: $report_name (age: $(( ($(date +%s) - file_mtime) / 60 ))m)"

    # Check if already processed by hash
    local report_hash=$(_sha256 "$report_file")
    if grep -q "^$report_hash$" "$PROCESSED_HASHES" 2>/dev/null; then
        log "DEBUG" "Already processed: $report_name"
        return 1
    fi

    return 0  # Should process
}

# File descriptor for receipt write lock (flock-based, OS-level atomic)
RECEIPT_LOCK_FD=9
RECEIPT_LOCK_FILE="$STATE_DIR/receipt_write.lock"

# Acquire exclusive lock for receipt writing via flock (prevents race conditions)
acquire_receipt_lock() {
    exec 9>"$RECEIPT_LOCK_FILE"
    if ! flock -w 5 $RECEIPT_LOCK_FD; then
        log "ERROR" "Receipt write lock acquisition failed after 5s (held by another process)"
        return 1
    fi
}

# Release receipt write lock
release_receipt_lock() {
    flock -u $RECEIPT_LOCK_FD 2>/dev/null || true
}

# Check if receipt content already exists (deduplication)
is_duplicate_receipt() {
    local receipt_json="$1"

    # Extract key identifying fields from receipt
    local dispatch_id=$(echo "$receipt_json" | jq -r '.dispatch_id // empty')
    local terminal=$(echo "$receipt_json" | jq -r '.terminal // empty')
    local timestamp=$(echo "$receipt_json" | jq -r '.timestamp // empty')
    local event_type=$(echo "$receipt_json" | jq -r '.event_type // .event // empty')

    if [ -z "$dispatch_id" ] || [ -z "$terminal" ]; then
        log "DEBUG" "Cannot deduplicate: missing dispatch_id or terminal"
        return 1  # Cannot determine, allow write
    fi

    # Check if identical receipt already exists in last 100 lines
    # (Same dispatch_id + terminal + event_type within 10 seconds)
    if [ -f "$RECEIPT_FILE" ]; then
        local existing=""
        existing=$(tail -100 "$RECEIPT_FILE" | grep -F "\"dispatch_id\":\"$dispatch_id\"" | grep -F "\"terminal\":\"$terminal\"" | grep -F "\"event_type\":\"$event_type\"" || :)

        if [ -n "$existing" ]; then
            # Check timestamp proximity (within 10 seconds = duplicate)
            local existing_ts=$(echo "$existing" | tail -1 | jq -r '.timestamp // empty')

            if [ -n "$existing_ts" ] && [ -n "$timestamp" ]; then
                # Simple timestamp comparison (ISO format lexicographic comparison works for proximity)
                # If timestamps are very close (same minute), it's likely a duplicate
                local ts_minute="${timestamp:0:16}"  # YYYY-MM-DDTHH:MM
                local existing_minute="${existing_ts:0:16}"

                if [ "$ts_minute" = "$existing_minute" ]; then
                    log "WARN" "Duplicate receipt detected: dispatch_id=$dispatch_id, terminal=$terminal, event=$event_type"
                    return 0  # Is duplicate
                fi
            fi
        fi
    fi

    return 1  # Not duplicate
}

# Flood protection with circuit breaker
check_flood_protection() {
    local queue_size="$1"

    # Check if flood protection is active — auto-clear if lock is stale
    if [ -f "$FLOOD_LOCKFILE" ]; then
        local lock_age=$(( $(date +%s) - $(stat -c %Y "$FLOOD_LOCKFILE" 2>/dev/null || stat -f %m "$FLOOD_LOCKFILE" 2>/dev/null || echo "0") ))
        if [ "$lock_age" -ge "$FLOOD_LOCK_MAX_AGE" ]; then
            log "INFO" "Flood lock expired after ${lock_age}s (max ${FLOOD_LOCK_MAX_AGE}s) — auto-clearing"
            rm -f "$FLOOD_LOCKFILE"
        else
            local remaining=$(( FLOOD_LOCK_MAX_AGE - lock_age ))
            log "WARN" "Flood protection active (${lock_age}s old, auto-clears in ${remaining}s). Manual: rm $FLOOD_LOCKFILE"
            return 1
        fi
    fi

    # Check queue size
    if [ "$queue_size" -gt "$FLOOD_THRESHOLD" ]; then
        log "ERROR" "FLOOD DETECTED! $queue_size reports in queue (threshold: $FLOOD_THRESHOLD)"
        touch "$FLOOD_LOCKFILE"

        # Alert T0
        local t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
        if [ -n "$t0_pane" ]; then
            echo "🚨 RECEIPT FLOOD PROTECTION ACTIVATED - $queue_size reports queued" | \
                tmux set-buffer && tmux paste-buffer -t "$t0_pane"
        fi

        return 1
    fi

    if [ "$queue_size" -gt "$((FLOOD_THRESHOLD / 2))" ]; then
        log "WARN" "Queue building up: $queue_size reports"
    fi

    return 0
}

# ─── Extracted helpers for process_single_report() ───────────────────────────
# Each function handles one responsibility. Shared receipt fields are extracted
# once via extract_receipt_fields() and read via _rf_* module-scope variables.

# Extract common receipt fields into module scope (one jq call batch).
# Sets: _rf_status, _rf_event_type, _rf_dispatch_id, _rf_timestamp, _rf_pr_id, _rf_report_path
extract_receipt_fields() {
    local json="$1"
    _rf_status=$(echo "$json" | jq -r '.status // "unknown"' 2>/dev/null)
    _rf_event_type=$(echo "$json" | jq -r '.event_type // .event // ""' 2>/dev/null)
    _rf_dispatch_id=$(echo "$json" | jq -r '.dispatch_id // ""' 2>/dev/null)
    _rf_timestamp=$(echo "$json" | jq -r '.timestamp // ""' 2>/dev/null)
    _rf_pr_id=$(echo "$json" | jq -r '.pr_id // ""' 2>/dev/null)
    _rf_report_path=$(echo "$json" | jq -r '.report_path // ""' 2>/dev/null)
}

# DRY helper: invoke update_progress_state.py with common receipt fields.
# Usage: _call_progress_update <track> [extra_flags...]
_call_progress_update() {
    local track="$1"; shift
    python3 "$SCRIPTS_DIR/update_progress_state.py" \
        --track "$track" \
        "$@" \
        --receipt-event "$_rf_event_type" \
        --receipt-status "$_rf_status" \
        --receipt-timestamp "$_rf_timestamp" \
        --receipt-dispatch-id "$_rf_dispatch_id" \
        --updated-by receipt_processor 2>&1
}

# Sub-helper: Update pattern usage counts in quality_intelligence.db (non-fatal).
_track_pattern_usage() {
    local receipt_json="$1"
    local used_hashes
    used_hashes=$(echo "$receipt_json" | jq -r '.used_pattern_hashes // empty | join(",")' 2>/dev/null)
    [ -z "$used_hashes" ] && return 0
    python3 - "$used_hashes" <<'PY'
import os, sys, sqlite3
from datetime import datetime
hashes = [h.strip().lower() for h in sys.argv[1].split(",") if h.strip()]
if not hashes:
    sys.exit(0)
state_dir = os.environ.get("VNX_STATE_DIR")
if not state_dir:
    raise RuntimeError("VNX_STATE_DIR not set")
db_path = os.path.join(state_dir, "quality_intelligence.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# O(1) lookup via indexed pattern_hash column in snippet_metadata
placeholders = ",".join("?" for _ in hashes)
rows = cur.execute(
    f"SELECT sm.snippet_rowid, sm.pattern_hash, cs.title, cs.usage_count "
    f"FROM snippet_metadata sm "
    f"JOIN code_snippets cs ON cs.rowid = sm.snippet_rowid "
    f"WHERE sm.pattern_hash IN ({placeholders})",
    hashes
).fetchall()

updated = 0
now = datetime.utcnow().isoformat()
for row in rows:
    new_count = int(row["usage_count"] or 0) + 1
    cur.execute("UPDATE code_snippets SET usage_count = ?, last_updated = ? WHERE rowid = ?",
                (new_count, now, row["snippet_rowid"]))
    cur.execute("""
        INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, last_used, confidence)
        VALUES (?, ?, ?, 1, ?, 1.0)
        ON CONFLICT(pattern_id) DO UPDATE SET
            used_count = used_count + 1,
            last_used = excluded.last_used,
            updated_at = CURRENT_TIMESTAMP
    """, (row["pattern_hash"], row["title"], row["pattern_hash"], now))
    updated += 1
if updated:
    conn.commit()
conn.close()
PY
}

# Sub-helper: Fallback success credit for recently offered patterns (non-fatal).
# When a receipt has status=success but NO used_pattern_hashes, give partial
# credit (success_count += 1) to patterns offered within the last 2 hours.
_track_pattern_success_fallback() {
    local receipt_json="$1"
    local status
    status=$(echo "$receipt_json" | jq -r '.status // ""' 2>/dev/null)
    local event_type
    event_type=$(echo "$receipt_json" | jq -r '.event_type // .event // ""' 2>/dev/null)
    local used_hashes
    used_hashes=$(echo "$receipt_json" | jq -r '.used_pattern_hashes // empty | join(",")' 2>/dev/null)

    # Only trigger on task_complete + success + no explicit used_pattern_hashes
    [ "$event_type" != "task_complete" ] && return 0
    [ "$status" != "success" ] && return 0
    [ -n "$used_hashes" ] && return 0

    python3 - <<'PY'
import os, sys, sqlite3
from datetime import datetime, timedelta
state_dir = os.environ.get("VNX_STATE_DIR")
if not state_dir:
    sys.exit(0)
db_path = os.path.join(state_dir, "quality_intelligence.db")
if not os.path.exists(db_path):
    sys.exit(0)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat()
rows = conn.execute('''
    SELECT pattern_id FROM pattern_usage
    WHERE last_offered >= ? AND last_offered IS NOT NULL
''', (cutoff,)).fetchall()
if not rows:
    conn.close()
    sys.exit(0)
now = datetime.utcnow().isoformat()
updated = 0
for row in rows:
    conn.execute('''
        UPDATE pattern_usage
        SET success_count = success_count + 1, updated_at = ?
        WHERE pattern_id = ?
    ''', (now, row['pattern_id']))
    updated += 1
if updated:
    conn.commit()
conn.close()
PY
}

# Section B: Append receipt, track patterns, mark processed, extract OIs.
# Returns 0 on success (new receipt), 1 on failure, 2 on duplicate.
append_and_track_receipt() {
    local report_path="$1"
    local report_name="$2"
    local receipt_json="$3"

    local append_output
    append_output=$(printf '%s\n' "$receipt_json" | python3 "$APPEND_RECEIPT_SCRIPT" 2>>"$PROCESSING_LOG")
    local append_rc=$?

    if [ $append_rc -ne 0 ]; then
        log_structured_failure "receipt_append_failed" "append_receipt.py rejected receipt" "report=$report_name"
        log "ERROR" "Failed to append receipt via append_receipt.py: $report_name"
        return 1
    fi

    # Check if append_receipt.py flagged this as duplicate
    if echo "$append_output" | grep -q '"status"[[:space:]]*:[[:space:]]*"duplicate"'; then
        log "INFO" "Duplicate receipt detected by append_receipt.py, skipping T0 notification: $report_name"
        return 2
    fi

    "$SCRIPTS_DIR/generate_t0_brief.sh" >/dev/null 2>&1 &
    log "DEBUG" "Triggered t0_brief.json regeneration (async)"

    _track_pattern_usage "$receipt_json"
    _track_pattern_success_fallback "$receipt_json"

    local report_hash=$(_sha256 "$report_path")
    echo "$report_hash" >> "$PROCESSED_HASHES"
    extract_timestamp "$report_path" > "$LAST_PROCESSED"

    if [ -f "$SCRIPTS_DIR/extract_open_items.py" ]; then
        if ! python3 "$SCRIPTS_DIR/extract_open_items.py" --report "$report_path" 2>&1 | tee -a "$PROCESSING_LOG"; then
            log "WARN" "Failed to extract open items from: $report_name (non-fatal)"
        fi
    fi

    return 0
}

# Section C: Shadow write terminal_state.json on completion/timeout/failure.
# Reads _rf_* variables. Non-fatal.
update_receipt_shadow_state() {
    local terminal="$1"

    if [ "$_rf_event_type" != "task_complete" ] && [ "$_rf_event_type" != "task_failed" ] && [ "$_rf_event_type" != "task_timeout" ]; then
        return 0
    fi

    local completion_status="idle"
    local clear_claim="true"
    local lease_seconds=""

    # no_confirmation timeout → blocked with lease to prevent immediate re-dispatch
    if [ "$_rf_event_type" = "task_timeout" ] && [ "$_rf_status" = "no_confirmation" ]; then
        completion_status="blocked"
        clear_claim="false"
        lease_seconds="$CONFIRMATION_GRACE_SECONDS"
    fi

    shadow_update_terminal_state "$terminal" "$completion_status" "$_rf_dispatch_id" "$_rf_timestamp" "$clear_claim" "$lease_seconds"
}

# Section C2: Move dispatch from active/ to completed/ on task finish.
# Reads _rf_* variables. Non-fatal.
_move_dispatch_to_completed() {
    if [ "$_rf_event_type" != "task_complete" ] && [ "$_rf_event_type" != "task_failed" ] && [ "$_rf_event_type" != "task_timeout" ]; then
        return 0
    fi
    [ -z "$_rf_dispatch_id" ] && return 0
    local src
    src=$(ls "$VNX_DISPATCH_DIR/active/${_rf_dispatch_id}"*.md 2>/dev/null | head -1)
    [ -z "$src" ] && return 0
    mv "$src" "$VNX_DISPATCH_DIR/completed/" 2>/dev/null && \
        log "DEBUG" "Dispatch moved: active → completed ($_rf_dispatch_id)" || \
        log "WARN" "Failed to move dispatch to completed: $_rf_dispatch_id"
}

# Section C2b helper: Auto-release canonical lease on terminal task events.
# Called for task_complete / task_failed / task_timeout.
# Uses release-on-receipt which resolves generation internally. Non-fatal.
_auto_release_lease_on_receipt() {
    local terminal="$1"
    local dispatch_id="$2"
    [ -z "$terminal" ] && return 0

    local _ror_args=(--terminal "$terminal")
    [ -n "$dispatch_id" ] && _ror_args+=(--dispatch-id "$dispatch_id")
    local release_result
    release_result=$(python3 "$SCRIPTS_DIR/runtime_core_cli.py" release-on-receipt "${_ror_args[@]}" 2>/dev/null)
    local rc=$?

    if [ $rc -eq 0 ]; then
        local reason
        reason=$(echo "$release_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('reason','ok'))" 2>/dev/null)
        log "INFO" "AUTO_LEASE_RELEASE: terminal=$terminal dispatch=${dispatch_id:-unset} reason=$reason"
    else
        local err
        err=$(echo "$release_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error') or d.get('reason','unknown'))" 2>/dev/null)
        log "WARN" "AUTO_LEASE_RELEASE: failed for terminal=$terminal dispatch=${dispatch_id:-unset} err=${err:-rc=$rc} (non-fatal)"
    fi
}

# Section D: Extract PR ID (3-tier) and attach evidence to open items.
# Reads _rf_* variables. Non-fatal.
attach_pr_evidence() {
    local receipt_json="$1"
    local report_path="$2"

    [ "$_rf_status" != "success" ] && return 0

    # Strategy 0: PR ID from receipt JSON (most reliable)
    local pr_id="$_rf_pr_id"
    local extraction_method=""
    if [ -n "$pr_id" ]; then
        extraction_method="receipt_json"
    fi

    # Strategy 1: Report metadata fallback
    if [ -z "$pr_id" ]; then
        pr_id=$(grep -E "^-?\s*\*\*PR-?ID\*\*:" "$report_path" | sed -E 's/.*:\s*//' | tr -d '[:space:]' 2>/dev/null)
        [ -n "$pr_id" ] && extraction_method="report_metadata"
    fi

    # Strategy 2: Filename parsing fallback
    if [ -z "$pr_id" ]; then
        pr_id=$(basename "$report_path" | grep -oE "pr[0-9]+" | tr '[:lower:]' '[:upper:]' | sed 's/PR/PR-/' 2>/dev/null)
        [ -n "$pr_id" ] && extraction_method="filename"
    fi

    if [ -z "$pr_id" ]; then
        log "DEBUG" "No PR-ID found in receipt - cannot attach evidence"
        return 0
    fi

    if [ ! -f "$SCRIPTS_DIR/open_items_manager.py" ]; then
        return 0
    fi

    log "INFO" "Attaching evidence to open items for $pr_id (via: $extraction_method, dispatch: $_rf_dispatch_id)"
    if python3 "$SCRIPTS_DIR/open_items_manager.py" attach-evidence \
        --pr "$pr_id" \
        --report "$report_path" \
        --dispatch "${_rf_dispatch_id:-unknown}" 2>&1 | tee -a "$PROCESSING_LOG"; then
        log "INFO" "📎 Evidence attached for $pr_id - T0 must review and close open items"
    else
        log "WARN" "Failed to attach evidence for $pr_id (non-fatal)"
    fi
}

# Sub-helper: Read active_dispatch_id from progress_state.yaml for a track.
_get_active_dispatch() {
    local track="$1"
    [ ! -f "$STATE_DIR/progress_state.yaml" ] && return 0
    python3 -c "
import yaml
try:
    with open('$STATE_DIR/progress_state.yaml', 'r') as f:
        data = yaml.safe_load(f)
        print(data.get('tracks', {}).get('$track', {}).get('active_dispatch_id', ''))
except (OSError, yaml.YAMLError, AttributeError, TypeError):
    print('')
" 2>/dev/null
}

_track_from_terminal() {
    local terminal="$1"
    case "$terminal" in
        T1) echo "A" ;;
        T2) echo "B" ;;
        T3) echo "C" ;;
        *) echo "" ;;
    esac
}

# Ensure completion/start receipts carry a concrete dispatch_id whenever possible.
_hydrate_receipt_identity() {
    local receipt_json="$1"
    local terminal="$2"

    local current_dispatch_id
    current_dispatch_id=$(echo "$receipt_json" | jq -r '.dispatch_id // ""' 2>/dev/null)
    local current_dispatch_id_lc
    current_dispatch_id_lc=$(printf '%s' "$current_dispatch_id" | tr '[:upper:]' '[:lower:]')
    case "$current_dispatch_id_lc" in
        ""|"unknown"|"none"|"null")
            ;;
        *)
            echo "$receipt_json"
            return 0
            ;;
    esac

    local track
    track=$(_track_from_terminal "$terminal")
    if [ -z "$track" ]; then
        echo "$receipt_json"
        return 0
    fi

    local active_dispatch_id
    active_dispatch_id=$(_get_active_dispatch "$track")
    local active_dispatch_id_lc
    active_dispatch_id_lc=$(printf '%s' "$active_dispatch_id" | tr '[:upper:]' '[:lower:]')
    case "$active_dispatch_id_lc" in
        ""|"unknown"|"none"|"null")
            echo "$receipt_json"
            return 0
            ;;
    esac

    # Also fill task_id when missing to keep completion evidence correlated.
    echo "$receipt_json" | jq --arg dispatch "$active_dispatch_id" '
        .dispatch_id = $dispatch
        | if ((.task_id // "" | ascii_downcase) == "unknown") or ((.task_id // "") == "") then .task_id = $dispatch else . end
    ' 2>/dev/null || echo "$receipt_json"
}

# Section E: Update progress_state.yaml based on receipt events.
# Reads _rf_* variables. Non-fatal.
update_track_progress() {
    local receipt_json="$1"
    local terminal="$2"

    [ ! -f "$SCRIPTS_DIR/update_progress_state.py" ] && return 0

    local track=""
    track=$(_track_from_terminal "$terminal")
    [ -z "$track" ] && return 0

    log "INFO" "PROGRESS_STATE: Processing receipt for Track $track (event=$_rf_event_type, status=$_rf_status)"
    local current_active_dispatch
    current_active_dispatch=$(_get_active_dispatch "$track")

    if [ "$_rf_event_type" = "task_complete" ] && [ "$_rf_status" = "success" ]; then
        _call_progress_update "$track" --status idle --dispatch-id ""
        log "INFO" "PROGRESS_STATE: Task completed → Track $track idle"
    elif [ "$_rf_event_type" = "task_started" ]; then
        _call_progress_update "$track"
        log "INFO" "PROGRESS_STATE: Recorded task_started for Track $track"
    elif [ "$_rf_event_type" = "task_timeout" ] && [ "$_rf_status" = "no_confirmation" ] \
         && [ -n "$_rf_dispatch_id" ] && [ "$_rf_dispatch_id" = "$current_active_dispatch" ]; then
        _call_progress_update "$track" --status blocked --dispatch-id "$_rf_dispatch_id"
        log "WARN" "PROGRESS_STATE: Track $track blocked (awaiting confirmation on $_rf_dispatch_id)"
    elif [ -n "$_rf_event_type" ] || [ -n "$_rf_status" ]; then
        _call_progress_update "$track" --status idle --dispatch-id ""
        log "INFO" "PROGRESS_STATE: Track $track idle (ready for new work)"
    fi
}

# Sub-helper: Build state line from t0_brief.json
# Accepts optional override_terminal arg — the terminal sending the receipt
# is by definition done working, so override its status to "idle".
_build_state_line() {
    local override_terminal="${1:-}"
    local brief_file="$STATE_DIR/t0_brief.json"
    [ ! -f "$brief_file" ] && return 0

    local t1_st t2_st t3_st q_pending q_active
    t1_st=$(jq -r '.terminals.T1.status // "idle"' "$brief_file" 2>/dev/null)
    t2_st=$(jq -r '.terminals.T2.status // "idle"' "$brief_file" 2>/dev/null)
    t3_st=$(jq -r '.terminals.T3.status // "idle"' "$brief_file" 2>/dev/null)
    q_pending=$(jq -r '.queues.pending // 0' "$brief_file" 2>/dev/null)
    q_active=$(jq -r '.queues.active // 0' "$brief_file" 2>/dev/null)

    # Override: terminal that sends receipt is idle
    case "$override_terminal" in
        T1) t1_st="idle" ;; T2) t2_st="idle" ;; T3) t3_st="idle" ;;
    esac

    echo "
📊 STATE: T1=$t1_st T2=$t2_st T3=$t3_st | Queue: pending=$q_pending active=$q_active"
}

# Sub-helper: Build quality line from sidecar (dispatch_id must match)
_build_quality_line() {
    local dispatch_id="$1"
    local quality_sidecar="$STATE_DIR/last_quality_summary.json"
    [ ! -f "$quality_sidecar" ] && return 0

    local qs_dispatch_id
    qs_dispatch_id=$(jq -r '.dispatch_id // ""' "$quality_sidecar" 2>/dev/null)
    [ "$qs_dispatch_id" != "$dispatch_id" ] && return 0

    local qs_decision qs_risk qs_blocker qs_warn qs_new_count qs_new_ids
    qs_decision=$(jq -r '.decision // "unknown"' "$quality_sidecar" 2>/dev/null)
    qs_risk=$(jq -r '.risk_score // 0' "$quality_sidecar" 2>/dev/null)
    qs_blocker=$(jq -r '.counts.blocker // 0' "$quality_sidecar" 2>/dev/null)
    qs_warn=$(jq -r '.counts.warn // 0' "$quality_sidecar" 2>/dev/null)
    qs_new_count=$(jq -r '.new_items // 0' "$quality_sidecar" 2>/dev/null)
    qs_new_ids=$(jq -r '.new_item_ids // [] | join(", ")' "$quality_sidecar" 2>/dev/null)

    # Build OI delta: resolved count and total open from open_items.json
    local oi_delta=""
    local oi_file="$STATE_DIR/open_items.json"
    if [ -f "$oi_file" ]; then
        oi_delta=$(python3 -c "
import json, sys
try:
    with open('$oi_file') as f:
        items = json.load(f)
    if not isinstance(items, list):
        items = items.get('items', [])
    total_open = sum(1 for i in items if i.get('status', '') not in ('done', 'resolved', 'closed'))
    resolved = sum(1 for i in items if i.get('status', '') in ('done', 'resolved', 'closed'))
    new_count = ${qs_new_count:-0}
    print(f' | OI: +{new_count} new, {resolved} resolved ({total_open} open)')
except Exception:
    pass
" 2>/dev/null)
    fi

    if [ "$qs_blocker" -gt 0 ] || [ "$qs_warn" -gt 0 ] 2>/dev/null; then
        local qs_parts=""
        [ "$qs_blocker" -gt 0 ] && qs_parts="${qs_blocker} blocking"
        if [ "$qs_warn" -gt 0 ]; then
            [ -n "$qs_parts" ] && qs_parts="${qs_parts}, "
            qs_parts="${qs_parts}${qs_warn} warn"
        fi

        # Build findings detail lines (max 5 for tmux readability)
        local qs_findings=""
        local findings_count
        findings_count=$(jq -r '.findings | length' "$quality_sidecar" 2>/dev/null || echo "0")
        if [ "$findings_count" -gt 0 ]; then
            qs_findings=$(jq -r '.findings[:5][] | "  → [\(.severity)] \(.file)\(if .symbol then ":\(.symbol)" else "" end) — \(.message)"' "$quality_sidecar" 2>/dev/null)
        fi

        echo "
⚠️ QUALITY [${qs_decision}|risk:${qs_risk}]: ${qs_parts}${oi_delta}"
        # Append finding details if available
        if [ -n "$qs_findings" ]; then
            echo "$qs_findings"
        fi
    else
        echo "
✅ QUALITY [${qs_decision}|risk:${qs_risk}]: clean${oi_delta}"
    fi
}

# Section E2: Check provenance quality (informational, non-fatal).
# Returns CLEAN, DIRTY_LOW, or DIRTY_HIGH as a signal for T0.
check_provenance_quality() {
    local receipt_json="$1"
    local is_dirty dirty_files
    is_dirty=$(echo "$receipt_json" | jq -r '.provenance.is_dirty // true' 2>/dev/null)
    dirty_files=$(echo "$receipt_json" | jq -r '.provenance.dirty_files // 0' 2>/dev/null)

    if [ "$is_dirty" = "false" ]; then
        echo "CLEAN"
    elif [ "$dirty_files" -gt 20 ]; then
        echo "DIRTY_HIGH"
    else
        echo "DIRTY_LOW"
    fi
}

# Section F (inner): Build enriched receipt message and paste to T0 tmux pane.
# Returns 0 on success, 1 if pane unreachable or paste failed.
# Reads _rf_* variables set by extract_receipt_fields().
_deliver_receipt_to_t0_pane() {
    local receipt_json="$1"
    local terminal="$2"

    local dispatch_id="${_rf_dispatch_id:-no-id}"

    # Ghost-receipt filter: skip pastes for stop-hook triggers without real dispatch context.
    # Prevents flooding T0 pane when long-running sessions emit interim Stop events.
    case "$dispatch_id" in
        unknown-*|no-id)
            log "INFO" "Skipping ghost receipt paste: dispatch_id=$dispatch_id"
            return 0
            ;;
    esac

    local t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
    if [ -z "$t0_pane" ]; then
        log "ERROR" "Could not find T0 pane - get_pane_id_smart returned empty"
        return 1
    fi

    local report_path="${_rf_report_path:-no-report}"

    # Determine next action based on status
    local next_action="Review report"
    case "$_rf_status" in
        "success") next_action="Progress to next gate" ;;
        "failure"|"error") next_action="Investigate failure" ;;
        "blocked") next_action="Resolve blocker" ;;
    esac

    # Map status: use "done" instead of "success" in footer (NDJSON unchanged)
    local footer_status="$_rf_status"
    [ "$footer_status" = "success" ] && footer_status="done"

    local state_line=$(_build_state_line "$terminal")
    local quality_line=$(_build_quality_line "$dispatch_id")

    # Git line: only show for DIRTY_HIGH (actionable); skip CLEAN and DIRTY_LOW
    local git_quality
    git_quality=$(check_provenance_quality "$receipt_json")
    local git_line=""
    if [ "$git_quality" = "DIRTY_HIGH" ]; then
        local git_ref
        git_ref=$(echo "$receipt_json" | jq -r '.provenance.git_ref // "?"' 2>/dev/null)
        git_line="
⚠️ Git: ${git_quality} (ref:${git_ref:0:8})"
    fi

    local receipt_msg="/t0-orchestrator 📨 RECEIPT:${terminal}:${footer_status} | ID: ${dispatch_id} | Next: ${next_action}${quality_line}${state_line}${git_line}
Report: ${report_path}"
    echo "$receipt_msg" | tmux load-buffer -
    if ! tmux paste-buffer -t "$t0_pane" 2>/dev/null; then
        log "ERROR" "Failed to paste receipt to T0 pane $t0_pane"
        return 1
    fi

    sleep 1
    tmux send-keys -t "$t0_pane" Enter
    sleep 0.3
    tmux send-keys -t "$t0_pane" Enter

    log "INFO" "Receipt delivered to T0 (pane: $t0_pane)"
}

# Section F: Outbox wrapper — write-first, then deliver.
# Persists receipt to receipts/pending/ before attempting tmux delivery.
# On success: moves file to receipts/processed/.
# On failure: leaves file in receipts/pending/ for _retry_pending_receipts().
send_receipt_to_t0() {
    local receipt_json="$1"
    local terminal="$2"

    # Ensure outbox directories exist
    mkdir -p "$RECEIPTS_PENDING_DIR" "$RECEIPTS_PROCESSED_DIR"

    # Write-first: persist before any delivery attempt (guarantees no data loss)
    local pending_file="$RECEIPTS_PENDING_DIR/$(date +%s)-${terminal}-$RANDOM.json"
    printf '%s\n' "$receipt_json" > "$pending_file"

    if _deliver_receipt_to_t0_pane "$receipt_json" "$terminal"; then
        mv "$pending_file" "$RECEIPTS_PROCESSED_DIR/$(basename "$pending_file")"
        return 0
    else
        log "WARN" "Receipt queued for retry: $(basename "$pending_file")"
        return 1
    fi
}

# Retry poller: attempt delivery of all receipts still in pending/.
# Called periodically from _poll_new_reports() and once on startup.
_retry_pending_receipts() {
    local pending_files=()
    while IFS= read -r -d '' f; do
        pending_files+=("$f")
    done < <(find "$RECEIPTS_PENDING_DIR" -name "*.json" -type f -print0 2>/dev/null)

    [ ${#pending_files[@]} -eq 0 ] && return 0

    log "INFO" "Retrying ${#pending_files[@]} pending receipt(s)..."
    for f in "${pending_files[@]}"; do
        local receipt_json
        receipt_json=$(cat "$f")
        local terminal
        terminal=$(echo "$receipt_json" | jq -r '.terminal // "unknown"' 2>/dev/null)
        # Re-extract _rf_* fields so _deliver_receipt_to_t0_pane() has the right context
        extract_receipt_fields "$receipt_json" 2>/dev/null || true
        if _deliver_receipt_to_t0_pane "$receipt_json" "$terminal"; then
            mv "$f" "$RECEIPTS_PROCESSED_DIR/$(basename "$f")"
            log "INFO" "Pending receipt delivered: $(basename "$f")"
        fi
    done
}

# ─── Main orchestrator ───────────────────────────────────────────────────────

# Process a single report — orchestrates extracted sub-functions.
process_single_report() {
    local report_path="$1"
    local report_name=$(basename "$report_path")

    log "INFO" "Processing: $report_name"

    # A. Extract terminal from filename or metadata fallback
    local terminal
    terminal="$(vnx_receipt_terminal_from_report_name "$report_name")"
    if [ -z "$terminal" ]; then
        local parsed_terminal=$(python3 "$SCRIPTS_DIR/report_parser.py" "$report_path" 2>/dev/null | jq -r '.terminal // empty')
        if [ -n "$parsed_terminal" ]; then
            terminal="$parsed_terminal"
            log "DEBUG" "Extracted terminal from metadata: $terminal"
        else
            log "WARN" "Could not determine terminal for: $report_name (skipping)"
            return 1
        fi
    fi

    # B. Parse report and generate receipt JSON
    local receipt_json=$(python3 "$SCRIPTS_DIR/report_parser.py" "$report_path" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$receipt_json" ]; then
        log_structured_failure "receipt_parse_failed" "report_parser.py failed to generate receipt JSON" "report=$report_name"
        log "ERROR" "Failed to parse report: $report_name"
        return 1
    fi

    # Fill missing dispatch identity from active track state when report metadata is incomplete.
    receipt_json=$(_hydrate_receipt_identity "$receipt_json" "$terminal")

    # Append receipt, track patterns, mark processed
    append_and_track_receipt "$report_path" "$report_name" "$receipt_json"
    local append_rc=$?

    if [ $append_rc -eq 1 ]; then
        return 1  # Hard failure
    fi

    # Extract common fields once for all downstream functions
    extract_receipt_fields "$receipt_json"

    # On duplicate (rc=2): skip downstream processing and T0 notification
    if [ $append_rc -eq 2 ]; then
        log "INFO" "Skipping downstream processing for duplicate: $report_name"
        return 0
    fi

    # C. Shadow write terminal state (non-fatal)
    update_receipt_shadow_state "$terminal"

    # C2. Move dispatch from active/ → completed/ on task finish (non-fatal)
    _move_dispatch_to_completed

    # C2b. Auto-release canonical lease on terminal receipt events (non-fatal).
    # Eliminates the need for manual `release-on-failure` after every worker receipt.
    # release-on-receipt looks up the current generation internally; idempotent.
    # Skip auto-release for no_confirmation timeouts: Section C intentionally keeps
    # the terminal blocked (canonical lease held) to prevent immediate re-dispatch.
    # Releasing the lease here would conflict with the blocked shadow state.
    if [ "$_rf_event_type" = "task_complete" ] || [ "$_rf_event_type" = "task_failed" ] \
       || { [ "$_rf_event_type" = "task_timeout" ] && [ "$_rf_status" != "no_confirmation" ]; }; then
        _auto_release_lease_on_receipt "$terminal" "$_rf_dispatch_id"
    fi

    # C3. Update dispatch_metadata outcome in quality DB (non-fatal)
    if [ -n "$_rf_dispatch_id" ] && { [ "$_rf_event_type" = "task_complete" ] || [ "$_rf_event_type" = "task_failed" ] || [ "$_rf_event_type" = "task_timeout" ]; }; then
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
    ('$_rf_status', '${report_path:-}', '$_rf_timestamp', '$_rf_dispatch_id')
)
conn.commit()
conn.close()
" 2>/dev/null || log "WARN" "Failed to update dispatch_metadata outcome (non-fatal)"

        # C3b. Compute and store CQS (non-fatal)
        python3 "$SCRIPTS_DIR/update_dispatch_cqs.py" --dispatch-id "$_rf_dispatch_id" 2>/dev/null \
            || log "WARN" "Failed to update dispatch CQS (non-fatal)"
    fi

    # D. Attach PR evidence on success (non-fatal)
    attach_pr_evidence "$receipt_json" "$report_path"

    # D2. Contract verification (Phase 2a: conditional on contract presence, non-fatal)
    # Runs lightweight claim checks only when the dispatch contains a ## Contract block.
    # Does NOT trigger test suites — only file existence, pattern, and git-diff checks.
    if [ -n "$_rf_dispatch_id" ] && [ "$_rf_event_type" = "task_complete" ]; then
        local verify_result
        verify_result=$(python3 "$SCRIPTS_DIR/verify_claims.py" --dispatch-id "$_rf_dispatch_id" --store 2>/dev/null)
        local verify_rc=$?
        if [ $verify_rc -eq 0 ]; then
            local verdict
            verdict=$(echo "$verify_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','unknown'))" 2>/dev/null)
            if [ "$verdict" = "pass" ]; then
                log "INFO" "CONTRACT VERIFIED: dispatch=$_rf_dispatch_id verdict=pass"
            elif [ "$verdict" = "no_contract" ]; then
                log "DEBUG" "No contract block in dispatch=$_rf_dispatch_id (Phase 2a: skip)"
            else
                log "INFO" "CONTRACT RESULT: dispatch=$_rf_dispatch_id verdict=$verdict"
            fi
        elif [ $verify_rc -eq 1 ]; then
            log "WARN" "CONTRACT FAILED: dispatch=$_rf_dispatch_id — one or more claims failed"
        else
            log "WARN" "Contract verification error for dispatch=$_rf_dispatch_id (rc=$verify_rc, non-fatal)"
        fi
    fi

    # E. Update progress state for tracked terminals (non-fatal)
    update_track_progress "$receipt_json" "$terminal"

    # F. Send enriched receipt to T0
    send_receipt_to_t0 "$receipt_json" "$terminal"

    # G. Update cost metrics (non-fatal, best-effort)
    python3 "$SCRIPTS_DIR/cost_tracker.py" 2>/dev/null \
        || log "WARN" "Failed to update cost metrics (non-fatal)"

    # H. Record pattern adoption signals for feedback loop (non-fatal, A-5)
    if [ -n "$_rf_dispatch_id" ] && [ "$_rf_event_type" = "task_complete" ]; then
        python3 "$SCRIPTS_DIR/gather_intelligence.py" record-adoption \
            "$_rf_dispatch_id" "${terminal:-unknown}" "$report_path" 2>/dev/null \
            || log "DEBUG" "Pattern adoption recording skipped (non-fatal)"
    fi

    return 0
}

# Process pending reports with rate limiting
process_pending_reports() {
    local processed_count=0
    local queue_count=0
    local cutoff=$(get_cutoff_time)

    log "INFO" "Scanning for reports newer than: $cutoff"

    # Scan unified_reports/ only — gate reports under headless/ are recorded
    # separately in state/review_gates/results/ and the gate runner returns
    # structured JSON synchronously. Scanning them here produces ghost
    # "RECEIPT:unknown:unknown" pings because they have no dispatch_id metadata.
    local pending_reports=()
    for report in "$UNIFIED_REPORTS"/*.md; do
        [ -f "$report" ] || continue
        if should_process_report "$report"; then
            pending_reports+=("$report")
            ((queue_count++))
        fi
    done

    # Check flood protection
    if ! check_flood_protection "$queue_count"; then
        log "ERROR" "Aborting due to flood protection"
        return 1
    fi

    if [ "$queue_count" -eq 0 ]; then
        log "INFO" "No pending reports to process"
        return 0
    fi

    log "INFO" "Processing $queue_count pending reports..."

    # Process with rate limiting
    local MAX_PROCESSED_MTIME=0
    for report in "${pending_reports[@]}"; do
        # Rate limiting check
        if [ "$processed_count" -ge "$RATE_LIMIT" ]; then
            log "INFO" "Rate limit reached ($RATE_LIMIT/min), pausing..."
            sleep 60
            processed_count=0
        fi

        if process_single_report "$report"; then
            ((processed_count++))
            local _mtime
            _mtime=$(stat -c %Y "$report" 2>/dev/null || stat -f %m "$report" 2>/dev/null)
            if [ -n "$_mtime" ] && [ "$_mtime" -gt "$MAX_PROCESSED_MTIME" ]; then
                MAX_PROCESSED_MTIME=$_mtime
            fi
        fi

        # Small delay between reports
        sleep 0.5
    done

    # Update watermark once with the highest mtime seen this sweep so that
    # non-chronological processing order cannot cause older files to be skipped.
    if [ "$MAX_PROCESSED_MTIME" -gt 0 ]; then
        echo "$MAX_PROCESSED_MTIME" > "$WATERMARK_FILE"
    fi

    log "INFO" "Processed $processed_count reports successfully"
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

# Watch for new reports via fswatch (preferred) or polling fallback.
# On startup, performs a quick catchup scan for reports created in the last
# 10 minutes to cover the gap between process restart and fswatch activation.
monitor_new_reports() {
    log "INFO" "Starting MONITOR mode - only new reports will be processed"
    date '+%Y%m%d-%H%M%S' > "$LAST_PROCESSED"

    # Startup catchup: process any reports from the last 10 minutes that
    # may have been written while the receipt processor was down (restart gap).
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

    # Startup: deliver any receipts that were pending when this process was last down
    _retry_pending_receipts

    # Polling mode — simple, no external processes, no orphan risk.
    _poll_new_reports

    # ── fswatch (disabled) ────────────────────────────────────────────
    # Previously used fswatch for sub-second file detection. Disabled
    # because the external process caused:
    #   - Orphan fswatch processes surviving parent death (PPID → 1)
    #   - Duplicate watchers from singleton race under memory pressure
    #   - fseventsd memory bloat (5+ GB observed with multiple watchers)
    #   - macOS FSEvents silently dropping rapid create+close events
    # Polling at 5s is effectively free and eliminates all of the above.
    # To re-enable: set VNX_USE_FSWATCH=1 and uncomment the block below.
    #
    # if [ "${VNX_USE_FSWATCH:-0}" = "1" ] && command -v fswatch >/dev/null 2>&1; then
    #     log "INFO" "Using fswatch for real-time monitoring"
    #     local fail_count=0
    #     while true; do
    #         local fifo="$STATE_DIR/.fswatch_fifo.$$"
    #         rm -f "$fifo"
    #         mkfifo "$fifo"
    #         fswatch -0 "$UNIFIED_REPORTS" > "$fifo" &
    #         local fswatch_pid=$!
    #         local last_sweep=$(date +%s)
    #         while true; do
    #             local path=""
    #             if IFS= read -r -d '' -t 5 path <&3; then
    #                 [[ "$path" == *.md ]] && should_process_report "$path" && process_single_report "$path"
    #             fi
    #             local now_ts=$(date +%s)
    #             if [ $((now_ts - last_sweep)) -ge 30 ]; then
    #                 for report in "$UNIFIED_REPORTS"/*.md; do
    #                     [ -f "$report" ] || continue
    #                     should_process_report "$report" && process_single_report "$report"
    #                 done
    #                 last_sweep=$now_ts
    #             fi
    #             if ! kill -0 "$fswatch_pid" 2>/dev/null; then
    #                 break
    #             fi
    #         done 3< "$fifo"
    #         wait "$fswatch_pid" 2>/dev/null
    #         local exit_code=$?
    #         rm -f "$fifo"
    #         fail_count=$((fail_count + 1))
    #         log "WARN" "fswatch exited (code=$exit_code). Failures: $fail_count/$FSWATCH_RETRIES"
    #         if [ "$fail_count" -ge "$FSWATCH_RETRIES" ]; then
    #             log "WARN" "fswatch unstable; falling back to polling mode"
    #             _poll_new_reports; return
    #         fi
    #         sleep "$FSWATCH_BACKOFF"
    #     done
    # fi
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
