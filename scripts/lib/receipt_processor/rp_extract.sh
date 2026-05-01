# shellcheck shell=bash
# rp_extract.sh - Receipt field extraction and quality/state line builders
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, $STATE_DIR
# Sets module-scope: _rf_status, _rf_event_type, _rf_dispatch_id, _rf_timestamp,
#                    _rf_pr_id, _rf_report_path

# Extract common receipt fields into module scope (one jq call batch).
extract_receipt_fields() {
    local json="$1"
    _rf_status=$(echo "$json" | jq -r '.status // "unknown"' 2>/dev/null)
    _rf_event_type=$(echo "$json" | jq -r '.event_type // .event // ""' 2>/dev/null)
    _rf_dispatch_id=$(echo "$json" | jq -r '.dispatch_id // ""' 2>/dev/null)
    _rf_timestamp=$(echo "$json" | jq -r '.timestamp // ""' 2>/dev/null)
    _rf_pr_id=$(echo "$json" | jq -r '.pr_id // ""' 2>/dev/null)
    _rf_report_path=$(echo "$json" | jq -r '.report_path // ""' 2>/dev/null)
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

# Compute OI delta string from open_items.json for a given new_count.
# Echoes the formatted delta string or nothing on error.
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

# Fetch up to 5 finding detail lines from quality sidecar for tmux display.
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

# Map receipt status to a human-readable next action string.
_drtp_get_next_action() {
    case "$1" in
        "success") echo "Progress to next gate" ;;
        "failure"|"error") echo "Investigate failure" ;;
        "blocked") echo "Resolve blocker" ;;
        *) echo "Review report" ;;
    esac
}

# Build the git warning line for DIRTY_HIGH provenance; echo nothing for CLEAN/DIRTY_LOW.
_drtp_build_git_line() {
    local receipt_json="$1"
    local git_quality
    git_quality=$(check_provenance_quality "$receipt_json")
    [ "$git_quality" != "DIRTY_HIGH" ] && return 0
    local git_ref
    git_ref=$(echo "$receipt_json" | jq -r '.provenance.git_ref // "?"' 2>/dev/null)
    printf '\n⚠️ Git: %s (ref:%s)' "$git_quality" "${git_ref:0:8}"
}
