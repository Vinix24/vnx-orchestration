# shellcheck shell=bash
# rp_delivery.sh - Receipt delivery to T0 pane via tmux + outbox retry
# Sourced by scripts/receipt_processor.sh
# Requires: log() from rp_logging.sh, _build_state_line/_build_quality_line/
#           _drtp_get_next_action/_drtp_build_git_line from rp_extract.sh,
#           extract_receipt_fields() from rp_extract.sh,
#           get_pane_id_smart() from pane_manager,
#           _rf_* fields, $RECEIPTS_PENDING_DIR, $RECEIPTS_PROCESSED_DIR

# Section F (inner): Build v2 enriched receipt message and paste to T0 tmux pane.
# Returns 0 on success, 1 if pane unreachable or paste failed.
# Reads _rf_* variables set by extract_receipt_fields().
_deliver_receipt_to_t0_pane() {
    local receipt_json="$1"
    local terminal="$2"

    local dispatch_id="${_rf_dispatch_id:-no-id}"

    # Ghost-receipt filter: skip pastes for stop-hook triggers without real dispatch context.
    case "$dispatch_id" in
        unknown-*|unknown|no-id|"")
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

    # --- v2 header line ---
    local status_upper pr_id pr_slug header_line
    pr_id="${_rf_pr_id:-}"
    pr_slug="${_rf_pr_title_slug:-$dispatch_id}"
    local exit_raw="${_rf_exit_code:-}"

    if [ -n "$exit_raw" ] && [ "$exit_raw" != "0" ]; then
        status_upper="FAILED (exit ${exit_raw})"
    else
        status_upper="DONE"
    fi

    if [ -n "$pr_id" ] && [ "$pr_id" != "none" ] && [ "$pr_id" != "None" ]; then
        header_line="📨 #${pr_id} PR-${pr_slug} — ${status_upper}"
    else
        header_line="📨 ${pr_slug} — ${status_upper}"
    fi

    # --- provider / lane line ---
    local provider_line="   Provider: ${_rf_provider:-?}-${_rf_model:-?}  |  Lane: ${_rf_lane:-?} (${_rf_isolation_mode:-?})"

    # --- files / tests line (omit tests if empty) ---
    local files_tests_line=""
    if [ -n "${_rf_files_changed}" ] || [ -n "${_rf_insertions}" ] || [ -n "${_rf_deletions}" ]; then
        local fc="${_rf_files_changed:-?}" ins="${_rf_insertions:-0}" del="${_rf_deletions:-0}"
        files_tests_line="   Files: ${fc} changed, +${ins}/-${del}"
        if [ -n "${_rf_tests_passed}" ]; then
            files_tests_line="${files_tests_line}  |  Tests: ${_rf_tests_passed} passed"
        fi
    fi

    # --- smart-context line (omit when empty) ---
    local smart_line=""
    [ -n "${_rf_smart_context}" ] && smart_line="   Smart-context: ${_rf_smart_context}"

    # --- gate line ---
    local gate_line
    if [ -n "${_rf_gate_name}" ]; then
        local top_adv="${_rf_gate_top_advisory:-}"
        local adv_part=""
        [ -n "$top_adv" ] && adv_part=" (${top_adv})"
        gate_line="   Gate: ${_rf_gate_name} → ${_rf_gate_blockers:-0} blockers / ${_rf_gate_advisories:-0} advisories${adv_part}"
    else
        gate_line="   Gate: pending"
    fi

    # --- next / report line ---
    local next_report_line="   Next: ${_rf_next_action:-verify}  |  Report: ${_rf_report_path:-no-report}"

    # Assemble full message (skip empty optional lines)
    local receipt_msg="/t0-orchestrator ${header_line}
${provider_line}"
    [ -n "$files_tests_line" ] && receipt_msg="${receipt_msg}
${files_tests_line}"
    [ -n "$smart_line" ] && receipt_msg="${receipt_msg}
${smart_line}"
    receipt_msg="${receipt_msg}
${gate_line}
${next_report_line}"

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
