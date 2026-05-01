# shellcheck shell=bash
# rp_delivery.sh - Receipt delivery to T0 pane via tmux + outbox retry
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, _build_state_line/_build_quality_line/
#           _drtp_get_next_action/_drtp_build_git_line from rp_extract.sh,
#           extract_receipt_fields() from rp_extract.sh,
#           get_pane_id_smart() from pane_manager_v2,
#           _rf_* fields, $RECEIPTS_PENDING_DIR, $RECEIPTS_PROCESSED_DIR

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

    local t0_pane
    t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
    if [ -z "$t0_pane" ]; then
        log "ERROR" "Could not find T0 pane - get_pane_id_smart returned empty"
        return 1
    fi

    local report_path="${_rf_report_path:-no-report}"
    local next_action
    next_action=$(_drtp_get_next_action "$_rf_status")
    local footer_status="$_rf_status"
    [ "$footer_status" = "success" ] && footer_status="done"

    local state_line quality_line git_line
    state_line=$(_build_state_line "$terminal")
    quality_line=$(_build_quality_line "$dispatch_id")
    git_line=$(_drtp_build_git_line "$receipt_json")

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
