# shellcheck shell=bash
# rp_state.sh - terminal_state.json shadow writers
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, $SCRIPTS_DIR, $CONFIRMATION_GRACE_SECONDS,
#           and _rf_* fields populated by extract_receipt_fields()

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
