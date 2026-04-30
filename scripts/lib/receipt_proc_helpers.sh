#!/bin/bash
# receipt_proc_helpers.sh — Utility helpers for receipt_processor_v4.sh
# Sourced by receipt_processor_v4.sh after variable initialization.
# All variables (SCRIPTS_DIR, STATE_DIR, etc.) must be set by caller.

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

_spr_get_cutoff_seconds() {
    if [ "$MODE" = "monitor" ]; then
        if [ -f "$WATERMARK_FILE" ]; then
            local cs
            cs=$(cat "$WATERMARK_FILE" 2>/dev/null)
            if ! [[ "$cs" =~ ^[0-9]+$ ]]; then
                cs=$(($(date +%s) - 86400))
            fi
            echo "$cs"
        else
            echo "$(($(date +%s) - 86400))"
        fi
    elif [ "$MODE" = "manual" ] && [ -f "$WATERMARK_FILE" ]; then
        local cs
        cs=$(cat "$WATERMARK_FILE" 2>/dev/null)
        if ! [[ "$cs" =~ ^[0-9]+$ ]]; then
            cs=$(($(date +%s) - (MAX_AGE_HOURS * 3600)))
        fi
        echo "$cs"
    else
        echo "$(($(date +%s) - (MAX_AGE_HOURS * 3600)))"
    fi
}

# Check if report should be processed
should_process_report() {
    local report_file="$1"
    local report_name
    report_name=$(basename "$report_file")

    local file_mtime
    file_mtime=$(stat -c %Y "$report_file" 2>/dev/null || stat -f %m "$report_file" 2>/dev/null)
    if [ -z "$file_mtime" ]; then
        log "ERROR" "Cannot get modification time for: $report_name"
        return 1
    fi

    local cutoff_seconds
    cutoff_seconds=$(_spr_get_cutoff_seconds)

    if [ "$file_mtime" -lt "$cutoff_seconds" ]; then
        local age_minutes=$(( ($(date +%s) - file_mtime) / 60 ))
        log "DEBUG" "Report too old: $report_name (age: ${age_minutes}m)"
        return 1
    fi

    log "DEBUG" "Report accepted: $report_name (age: $(( ($(date +%s) - file_mtime) / 60 ))m)"

    local report_hash
    report_hash=$(_sha256 "$report_file")
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
