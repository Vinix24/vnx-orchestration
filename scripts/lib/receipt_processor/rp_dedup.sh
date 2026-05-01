# shellcheck shell=bash
# rp_dedup.sh - Report dedup and flood protection
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, _spr_get_cutoff_seconds() from rp_time.sh,
#           _sha256() from main, $PROCESSED_HASHES, $RECEIPT_FILE, $FLOOD_LOCKFILE,
#           $FLOOD_LOCK_MAX_AGE, $FLOOD_THRESHOLD, get_pane_id_smart() from pane_manager_v2

# Check if report should be processed
should_process_report() {
    local report_file="$1"
    local report_name
    report_name=$(basename "$report_file")

    # PERMANENT FIX: Use file modification time, NOT filename timestamp
    # This prevents reports with old dispatch timestamps from being rejected
    # when they are actually created NOW
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
