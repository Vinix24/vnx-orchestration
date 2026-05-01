# shellcheck shell=bash
# rp_append.sh - Append receipt + track patterns + mark processed
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() and log_structured_failure() from rp_logging.sh,
#           _track_pattern_usage() / _track_pattern_success_fallback() from rp_pattern.sh,
#           extract_timestamp() from rp_time.sh, _sha256() from main,
#           $APPEND_RECEIPT_SCRIPT, $PROCESSING_LOG, $SCRIPTS_DIR,
#           $PROCESSED_HASHES, $LAST_PROCESSED

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
