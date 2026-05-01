# shellcheck shell=bash
# rp_dispatch.sh - Dispatch lifecycle helpers (move-to-completed, lease release, PR evidence)
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, $VNX_DISPATCH_DIR, $SCRIPTS_DIR, $PROCESSING_LOG,
#           and _rf_* fields populated by extract_receipt_fields()

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
