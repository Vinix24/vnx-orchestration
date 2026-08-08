# shellcheck shell=bash
# rp_append.sh - Append receipt + track patterns + mark processed
# Sourced by scripts/receipt_processor.sh
# Requires: log() and log_structured_failure() from rp_logging.sh,
#           _track_pattern_usage() / _track_pattern_success_fallback() from rp_pattern.sh,
#           record_rejection_and_maybe_deadletter() from rp_deadletter.sh,
#           extract_timestamp() from rp_time.sh, _sha256() from main,
#           $APPEND_RECEIPT_SCRIPT, $PROCESSING_LOG, $SCRIPTS_DIR, $STATE_DIR,
#           $PROCESSED_HASHES, $LAST_PROCESSED

# Section B: Append receipt, track patterns, mark processed, extract OIs.
# Returns 0 on success (new receipt), 1 on failure, 2 on duplicate.
append_and_track_receipt() {
    local report_path="$1"
    local report_name="$2"
    local receipt_json="$3"

    # OI-1085/1086: capture append_receipt.py stderr separately so the
    # structured error code it emits ({"code":"missing_model", ...}) can be
    # extracted for dead-letter accounting; the stderr content itself is
    # still mirrored into the processing log exactly as before.
    local append_err_file="$STATE_DIR/.append_stderr.$$"
    local append_output
    append_output=$(printf '%s\n' "$receipt_json" | python3 "$APPEND_RECEIPT_SCRIPT" 2>"$append_err_file")
    local append_rc=$?

    if [ $append_rc -ne 0 ]; then
        cat "$append_err_file" >> "$PROCESSING_LOG" 2>/dev/null || :
        local rejection_code
        rejection_code=$(jq -R 'fromjson? | .code // empty' "$append_err_file" 2>/dev/null | tail -1)
        rm -f "$append_err_file"
        # A deterministic validation refusal (e.g. missing_model) can never
        # succeed on retry — count it and quarantine after N identical
        # refusals instead of looping forever (OI-1085/1086).
        record_rejection_and_maybe_deadletter "$report_path" "$report_name" "$rejection_code"
        log_structured_failure "receipt_append_failed" "append_receipt.py rejected receipt" "report=$report_name code=${rejection_code:-unknown}"
        log "ERROR" "Failed to append receipt via append_receipt.py: $report_name"
        return 1
    fi
    rm -f "$append_err_file"

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
