# shellcheck shell=bash
# rp_delivery.sh - Receipt outbox (pull model, ADR-035 §5.3/§9 PR-8)
# Sourced by scripts/receipt_processor.sh
# Requires: log() from rp_logging.sh, $RECEIPTS_PENDING_DIR, $RECEIPTS_PROCESSED_DIR
#
# There is no push to the T0 pane: the ndjson append happens upstream in
# append_and_track_receipt(), and T0 reads open_outcomes instead
# (scripts/receipt_query.py open-outcomes, docs/core/DISPATCH_RULES.md §13).
# This file only keeps the outbox bookkeeping: a receipt is persisted to
# receipts/pending/ first and then moved to receipts/processed/.

# Persist a receipt to receipts/pending/, then move it to receipts/processed/.
# Returns 0 when the receipt reached processed/, 1 when it did not (the file
# then stays in pending/ for _retry_pending_receipts()).
send_receipt_to_t0() {
    local receipt_json="$1"
    local terminal="$2"

    mkdir -p "$RECEIPTS_PENDING_DIR" "$RECEIPTS_PROCESSED_DIR"

    # Write-first: persist before the move (guarantees no data loss).
    # SC2155: declare and assign separately so the $(date) exit status isn't masked.
    local pending_file
    pending_file="$RECEIPTS_PENDING_DIR/$(date +%s)-${terminal}-$RANDOM.json"
    if ! printf '%s\n' "$receipt_json" > "$pending_file"; then
        log "ERROR" "Failed to persist receipt to $pending_file"
        return 1
    fi

    if mv "$pending_file" "$RECEIPTS_PROCESSED_DIR/$(basename "$pending_file")"; then
        return 0
    fi
    log "WARN" "Receipt queued for retry: $(basename "$pending_file")"
    return 1
}

# Retry poller: move every receipt still in pending/ to processed/. A file that
# fails to move stays in pending/ and is retried on the next sweep.
# Called periodically from _poll_new_reports() and once on startup.
_retry_pending_receipts() {
    local f moved=0 failed=0
    while IFS= read -r -d '' f; do
        if mv "$f" "$RECEIPTS_PROCESSED_DIR/$(basename "$f")" 2>/dev/null; then
            moved=$((moved + 1))
        else
            failed=$((failed + 1))
            log "WARN" "Could not move pending receipt: $(basename "$f")"
        fi
    done < <(find "$RECEIPTS_PENDING_DIR" -name "*.json" -type f -print0 2>/dev/null)

    if [ $((moved + failed)) -gt 0 ]; then
        log "INFO" "Pending receipts swept: moved=$moved failed=$failed"
    fi
    [ "$failed" -eq 0 ]
}
