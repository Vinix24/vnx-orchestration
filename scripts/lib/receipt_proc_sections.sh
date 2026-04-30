#!/bin/bash
# receipt_proc_sections.sh — Business logic sections B/C/D/E/F for receipt_processor_v4.sh
# Sourced by receipt_processor_v4.sh after receipt_proc_helpers.sh.
# All variables (SCRIPTS_DIR, STATE_DIR, etc.) must be set by caller.

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

_bql_compute_oi_delta() {
    local new_count="${1:-0}"
    local oi_file="$STATE_DIR/open_items.json"
    [ ! -f "$oi_file" ] && return 0
    python3 -c "
import json, sys
try:
    with open('$oi_file') as f:
        items = json.load(f)
    if not isinstance(items, list):
        items = items.get('items', [])
    total_open = sum(1 for i in items if i.get('status', '') not in ('done', 'resolved', 'closed'))
    resolved = sum(1 for i in items if i.get('status', '') in ('done', 'resolved', 'closed'))
    print(f' | OI: +${new_count} new, {resolved} resolved ({total_open} open)')
except Exception:
    pass
" 2>/dev/null
}

_bql_fetch_findings() {
    local quality_sidecar="$1"
    local findings_count
    findings_count=$(jq -r '.findings | length' "$quality_sidecar" 2>/dev/null || echo "0")
    [ "$findings_count" -gt 0 ] || return 0
    jq -r '.findings[:5][] | "  → [\(.severity)] \(.file)\(if .symbol then ":\(.symbol)" else "" end) — \(.message)"' "$quality_sidecar" 2>/dev/null
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

    local oi_delta
    oi_delta=$(_bql_compute_oi_delta "$qs_new_count")

    if [ "$qs_blocker" -gt 0 ] || [ "$qs_warn" -gt 0 ] 2>/dev/null; then
        local qs_parts=""
        [ "$qs_blocker" -gt 0 ] && qs_parts="${qs_blocker} blocking"
        if [ "$qs_warn" -gt 0 ]; then
            [ -n "$qs_parts" ] && qs_parts="${qs_parts}, "
            qs_parts="${qs_parts}${qs_warn} warn"
        fi
        local qs_findings
        qs_findings=$(_bql_fetch_findings "$quality_sidecar")
        echo "
⚠️ QUALITY [${qs_decision}|risk:${qs_risk}]: ${qs_parts}${oi_delta}"
        [ -n "$qs_findings" ] && echo "$qs_findings"
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

_drtp_get_next_action() {
    case "$1" in
        "success") echo "Progress to next gate" ;;
        "failure"|"error") echo "Investigate failure" ;;
        "blocked") echo "Resolve blocker" ;;
        *) echo "Review report" ;;
    esac
}

_drtp_build_git_line() {
    local receipt_json="$1"
    local git_quality
    git_quality=$(check_provenance_quality "$receipt_json")
    [ "$git_quality" != "DIRTY_HIGH" ] && return 0
    local git_ref
    git_ref=$(echo "$receipt_json" | jq -r '.provenance.git_ref // "?"' 2>/dev/null)
    printf '\n⚠️ Git: %s (ref:%s)' "$git_quality" "${git_ref:0:8}"
}

# Section F (inner): Build enriched receipt message and paste to T0 tmux pane.
# Returns 0 on success, 1 if pane unreachable or paste failed.
# Reads _rf_* variables set by extract_receipt_fields().
_deliver_receipt_to_t0_pane() {
    local receipt_json="$1"
    local terminal="$2"

    local dispatch_id="${_rf_dispatch_id:-no-id}"

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
