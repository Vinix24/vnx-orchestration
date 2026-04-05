#!/bin/bash
# Dispatcher V8 Minimal - Native Skills + Instruction-Only Dispatch
# BREAKING CHANGE: Assumes skills loaded natively at session start
# Only sends: skill activation + instruction + receipt (no template compilation)

set -euo pipefail

# Ensure tmux/jq are available when launched via nohup/setsid
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/dispatch_metadata.sh"
source "$SCRIPT_DIR/lib/provider_routing.sh"
source "$SCRIPT_DIR/lib/model_routing.sh"
source "$SCRIPT_DIR/lib/input_mode_guard.sh"

# Configuration
PROJECT_ROOT="${PROJECT_ROOT}"
VNX_DIR="$VNX_HOME"

# --- Runtime Core defaults (PR-5 cutover) ---
# VNX_RUNTIME_PRIMARY=1: broker + canonical lease are the authoritative path.
# Set VNX_RUNTIME_PRIMARY=0 to revert to legacy-only mode (rollback).
VNX_RUNTIME_PRIMARY="${VNX_RUNTIME_PRIMARY:-1}"
VNX_BROKER_SHADOW="${VNX_BROKER_SHADOW:-0}"
VNX_CANONICAL_LEASE_ACTIVE="${VNX_CANONICAL_LEASE_ACTIVE:-1}"
export VNX_RUNTIME_PRIMARY VNX_BROKER_SHADOW VNX_CANONICAL_LEASE_ACTIVE

# Source the singleton enforcer
source "$VNX_DIR/scripts/singleton_enforcer.sh"

# Enforce singleton - will exit if another instance is running
enforce_singleton "dispatcher_v8_minimal"

# Configuration
CLAUDE_DIR="$PROJECT_ROOT/.claude"
DISPATCH_DIR="$VNX_DISPATCH_DIR"
QUEUE_DIR="$DISPATCH_DIR/queue"
PENDING_DIR="$DISPATCH_DIR/pending"
ACTIVE_DIR="$DISPATCH_DIR/active"
COMPLETED_DIR="$DISPATCH_DIR/completed"
REJECTED_DIR="$DISPATCH_DIR/rejected"
STATE_DIR="$VNX_STATE_DIR"
TERMINALS_DIR="$CLAUDE_DIR/terminals"
LOG_FILE="$VNX_LOGS_DIR/dispatcher_v8.log"
PROGRESS_FILE="$STATE_DIR/progress.yaml"
RUN_ID=$(date +%s)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Initialize log (avoid process substitution issues under nohup)
mkdir -p "$(dirname "$LOG_FILE")"
exec >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dispatcher V8 MINIMAL starting..."

# Initialize directories
for dir in "$QUEUE_DIR" "$PENDING_DIR" "$ACTIVE_DIR" "$COMPLETED_DIR" "$REJECTED_DIR"; do
    mkdir -p "$dir"
done

# Source decomposed modules (order: logging first, lifecycle second,
# deliver third — depends on lifecycle; create fourth — depends on deliver)
source "$SCRIPT_DIR/lib/dispatch_logging.sh"
source "$SCRIPT_DIR/lib/dispatch_lifecycle.sh"
source "$SCRIPT_DIR/lib/dispatch_deliver.sh"
source "$SCRIPT_DIR/lib/dispatch_create.sh"

# Large-payload threshold (referenced by dispatch_deliver.sh tmux_load_buffer_safe)
VNX_DISPATCH_MAX_INLINE="${VNX_DISPATCH_MAX_INLINE:-51200}"  # 50KB default
VNX_DISPATCH_PAYLOAD_DIR="${VNX_DATA_DIR:-/tmp}/dispatch_payloads"

# Source smart pane manager for self-healing pane discovery
source "$VNX_DIR/scripts/pane_manager_v2.sh"

# ===== METADATA EXTRACTION FUNCTIONS (from V7) =====

extract_track() { vnx_dispatch_extract_track "$1"; }
extract_cognition() { vnx_dispatch_extract_cognition "$1"; }
extract_priority() { vnx_dispatch_extract_priority "$1"; }
extract_agent_role() { vnx_dispatch_extract_agent_role "$1"; }
normalize_role() { vnx_dispatch_normalize_role "$1"; }
extract_phase() { vnx_dispatch_extract_phase "$1"; }
extract_new_gate() { vnx_dispatch_extract_new_gate "$1"; }
extract_task_id() { vnx_dispatch_extract_task_id "$1" "$2"; }
extract_pr_id() { vnx_dispatch_extract_pr_id "$1"; }

# ===== MODE CONTROL FUNCTIONS (from V7 Track 2b) =====

# Terminal provider resolution (Claude Code vs Codex CLI)
get_terminal_provider() {
    local terminal_id="$1"  # T0|T1|T2|T3
    local env_key="VNX_${terminal_id}_PROVIDER"
    local env_provider="${!env_key:-}"
    if [ -n "$env_provider" ]; then
        echo "$env_provider" | tr '[:upper:]' '[:lower:]'; return 0
    fi
    if command -v jq >/dev/null 2>&1 && [ -f "$STATE_DIR/panes.json" ]; then
        local provider terminal_lower
        terminal_lower=$(echo "$terminal_id" | tr '[:upper:]' '[:lower:]')
        if ! provider=$(jq -r ".${terminal_id}.provider // .${terminal_lower}.provider // empty" "$STATE_DIR/panes.json" 2>/dev/null); then
            provider=""
            log_structured_failure "pane_provider_lookup_failed" "Failed to resolve terminal provider from panes.json" "terminal=$terminal_id"
        fi
        if [ -n "$provider" ] && [ "$provider" != "null" ]; then
            echo "$provider" | tr '[:upper:]' '[:lower:]'; return 0
        fi
    fi
    echo "claude_code"
}

get_context_reset_command() {
    local provider="$1"
    case "$provider" in
        codex_cli|codex) echo "/new" ;;
        *) echo "/clear" ;;
    esac
}

extract_mode() {
    local mode
    mode=$(vnx_dispatch_extract_mode "$1")
    if [ "$mode" = "planning" ]; then
        log "V8: Planning mode detected - will activate Opus and @planner skill"
    fi
    echo "$mode"
}

extract_clear_context() { vnx_dispatch_extract_clear_context "$1"; }
extract_force_normal_mode() { vnx_dispatch_extract_force_normal_mode "$1"; }
extract_requires_model() { vnx_dispatch_extract_requires_model "$1"; }
extract_requires_model_strength() { vnx_dispatch_extract_requires_model_strength "$1"; }
extract_requires_provider() { vnx_dispatch_extract_requires_provider "$1"; }
extract_requires_provider_strength() { vnx_dispatch_extract_requires_provider_strength "$1"; }

# ===== END MODE CONTROL FUNCTIONS =====

# ===== V8 CORE DISPATCH FUNCTION =====

# dispatch_with_skill_activation — thin wrapper calling the 4 module functions.
dispatch_with_skill_activation() {
    local dispatch_file="$1" track="$2" agent_role="$3"
    local intelligence_data="${4:-}" dispatch_id="${5:-}"
    if [ -z "$dispatch_id" ]; then dispatch_id="$(basename "$dispatch_file" .md)"; fi

    prepare_dispatch_payload "$dispatch_file" "$track" "$agent_role" "$intelligence_data" "$dispatch_id" || return 1

    acquire_dispatch_lease "$dispatch_file" "$track" \
        "$_DP_TERMINAL_ID" "$dispatch_id" "$_DP_SKILL_NAME" "$_DP_GATE" "$_DP_COMPLETE_PROMPT" || return 1

    deliver_dispatch_to_terminal "$dispatch_file" "$track" "$agent_role" "$dispatch_id" \
        "$_DP_TARGET_PANE" "$_DP_TERMINAL_ID" "$_DP_PROVIDER" \
        "$_DP_COMPLETE_PROMPT" "$_DP_SKILL_COMMAND" || return 1

    finalize_dispatch_delivery "$dispatch_file" "$track" "$_DP_TERMINAL_ID" "$dispatch_id" \
        "$_DP_PR_ID" "$_DP_GATE" "$agent_role" "$_DP_INSTRUCTION_CONTENT" "$intelligence_data"
}

# ===== INTELLIGENCE INTEGRATION (V7.4) =====

# Globals set by validate_dispatch_preconditions
_PD_TRACK="" _PD_COGNITION="" _PD_PRIORITY="" _PD_GATE="" _PD_DISPATCH_ID="" _PD_TARGET_TERMINAL=""

# validate_dispatch_preconditions — pre-delivery guard: skill/role/agent validation + metadata.
# Sets: _PD_TRACK _PD_COGNITION _PD_PRIORITY _PD_GATE _PD_DISPATCH_ID _PD_TARGET_TERMINAL
# Returns 1 (caller should continue) if dispatch should be skipped.
validate_dispatch_preconditions() {
    local dispatch="$1"
    local agent_role
    agent_role=$(extract_agent_role "$dispatch")
    log "V8: Processing dispatch: $(basename "$dispatch") (Role: $agent_role)"

    if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
        log "V8 WARNING: Dispatch $(basename "$dispatch") blocked due to invalid skill (waiting for edit)"
        return 1
    fi

    if [ -z "$agent_role" ] || [ "$agent_role" = "none" ] || [ "$agent_role" = "None" ]; then
        log "V8 ERROR: Empty or 'none' role — dispatch blocked at pre-validation: $(basename "$dispatch")"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            printf '\n\n[SKILL_INVALID] Role is empty or '"'"'none'"'"'. Set a valid Role and remove this marker to retry.\n' >> "$dispatch"
        fi
        return 1
    fi

    local _mapped_skill_pre
    _mapped_skill_pre="$(map_role_to_skill "$agent_role" 2>/dev/null || echo "$agent_role")"
    if ! python3 "$VNX_DIR/scripts/validate_skill.py" "$_mapped_skill_pre" >/dev/null 2>&1; then
        log "V8 ERROR: Skill '@${_mapped_skill_pre}' failed registry validation — blocking dispatch before terminal operations"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            printf '\n\n[SKILL_INVALID] Skill '"'"'@%s'"'"' not found in registry. Update Role and remove this marker to retry.\n' "$_mapped_skill_pre" >> "$dispatch"
        fi
        return 1
    fi
    log "V8 SKILL_VALIDATION: Skill '@${_mapped_skill_pre}' validated against registry"

    # V7.4 INTELLIGENCE: Validate agent — command failure blocks dispatch (RES-A3)
    if [ -n "$agent_role" ] && [ "$agent_role" != "none" ] && [ "$agent_role" != "None" ]; then
        local validation_rc=0 validation_result
        set +e
        validation_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" validate "$agent_role" 2>&1)
        validation_rc=$?
        set -e
        if [ "$validation_rc" -ne 0 ]; then
            log_structured_failure "agent_validation_dependency_failed" "Agent validation command failed; dispatch blocked" "role=$agent_role rc=$validation_rc"
            if ! grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
                echo -e "\n\n[DEPENDENCY_ERROR] gather_intelligence validate failed (rc=$validation_rc). Resolve runtime dependency and retry.\n" >> "$dispatch"
            fi
            return 1
        fi
        if echo "$validation_result" | grep -q '"valid": false'; then
            log "V8 ERROR: Agent validation failed for '$agent_role'"
            log "Validation result: $validation_result"
            local suggested
            suggested=$(echo "$validation_result" | grep -o '"suggestion": "[^"]*"' | cut -d'"' -f4)
            log "Suggested agent: $suggested"
            if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
                echo -e "\n\n[SKILL_INVALID] Skill '$agent_role' not found. Suggested: '$suggested'. Update Role and remove this marker to retry.\n" >> "$dispatch"
            fi
            return 1
        else
            log "V8: Agent validated: $agent_role"
        fi
    fi

    _PD_TRACK=$(extract_track "$dispatch")
    _PD_COGNITION=$(extract_cognition "$dispatch")
    _PD_PRIORITY=$(extract_priority "$dispatch")
    _PD_GATE=$(extract_new_gate "$dispatch")
    _PD_DISPATCH_ID="$(basename "$dispatch" .md)"

    if [ -z "$_PD_TRACK" ]; then
        log "V8 WARNING: No track found in dispatch, skipping"
        mv "$dispatch" "$REJECTED_DIR/"; return 1
    fi
    if [ "$_PD_TRACK" = "0" ] || [ "$_PD_TRACK" = "T0" ]; then
        log "V8 ERROR: Attempting to dispatch to T0 - BLOCKED"
        mv "$dispatch" "$REJECTED_DIR/"; return 1
    fi

    _PD_TARGET_TERMINAL="$(track_to_terminal "$_PD_TRACK")"
    if [ -z "$_PD_TARGET_TERMINAL" ]; then
        log "V8 ERROR: Invalid track '$_PD_TRACK' for dispatch $(basename "$dispatch")"
        mv "$dispatch" "$REJECTED_DIR/"; return 1
    fi

    if ! terminal_lock_allows_dispatch "$_PD_TARGET_TERMINAL" "$_PD_DISPATCH_ID"; then
        log "V8 LOCK: deferring $(basename "$dispatch") until terminal $_PD_TARGET_TERMINAL is unlocked"
        return 1
    fi
    return 0
}

# Global set by gather_dispatch_intelligence
_PD_INTEL_RESULT=""

# gather_dispatch_intelligence — gather intelligence for dispatch (V7.4).
# Sets: _PD_INTEL_RESULT. Returns 1 if DEPENDENCY_ERROR blocks dispatch.
gather_dispatch_intelligence() {
    local dispatch="$1" agent_role="$2" track="$3" dispatch_id="$4" gate="$5"
    _PD_INTEL_RESULT=""
    [ -f "$VNX_DIR/scripts/gather_intelligence.py" ] || return 0

    log "V8 INTELLIGENCE: Gathering intelligence for dispatch"
    local task_description terminal
    task_description=$(extract_instruction_content "$dispatch")
    terminal=$(track_to_terminal "$track")
    local intel_rc=0
    set +e
    _PD_INTEL_RESULT=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" gather "$task_description" "$terminal" "$agent_role" "$gate" 2>&1)
    intel_rc=$?
    set -e

    if [ "$intel_rc" -ne 0 ]; then
        log_structured_failure "intelligence_gather_failed" "Intelligence gather command failed; dispatch blocked" "dispatch=$dispatch_id terminal=$terminal rc=$intel_rc"
        if ! grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
            echo -e "\n\n[DEPENDENCY_ERROR] gather_intelligence gather failed (rc=$intel_rc). Resolve runtime dependency and retry.\n" >> "$dispatch"
        fi
        return 1
    fi

    local pattern_count prevention_rules
    pattern_count=$(echo "$_PD_INTEL_RESULT" | grep '"pattern_count":' | grep -o '[0-9]*' | head -1 || echo "0")
    prevention_rules=$(echo "$_PD_INTEL_RESULT" | grep '"prevention_rule_count":' | grep -o '[0-9]*' | head -1 || echo "0")
    log "V8 INTELLIGENCE: Gathered $pattern_count patterns, $prevention_rules rules → injecting into prompt"
    return 0
}

# execute_and_classify_dispatch — call dispatch_with_skill_activation and classify result.
# Returns 0 on success, 1 to skip/continue.
execute_and_classify_dispatch() {
    local dispatch="$1" track="$2" agent_role="$3" intel_result="$4" dispatch_id="$5"

    if ! dispatch_with_skill_activation "$dispatch" "$track" "$agent_role" "$intel_result" "$dispatch_id"; then
        if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            log "V8 WARNING: Dispatch blocked due to invalid skill (waiting for edit): $(basename "$dispatch")"; return 1
        fi
        if grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
            log "V8 WARNING: Dispatch blocked due to dependency error (waiting for resolution): $(basename "$dispatch")"; return 1
        fi
        # RC-3: Only reject when explicit [REJECTED:] marker was written by the failure path
        if grep -q "\[REJECTED:" "$dispatch"; then
            log "V8 ERROR: Dispatch permanently rejected: $(basename "$dispatch")"
            [ -f "$dispatch" ] && mv "$dispatch" "$REJECTED_DIR/"
            return 1
        fi
        log "V8 INFO: Dispatch failed with requeueable condition — deferring to pending: $(basename "$dispatch")"
        return 1
    fi
    return 0
}

_cleanup_stuck_dispatches() {
    while IFS= read -r stuck_file; do
        if [ -f "$stuck_file" ]; then
            log "V8: Moving stuck file to completed: $(basename "$stuck_file")"
            if ! mv "$stuck_file" "$COMPLETED_DIR/" 2>/dev/null; then
                log_structured_failure "stuck_file_move_failed" "Failed to move stuck file to completed" "file=$stuck_file"
            fi
        fi
    done < <(find "$ACTIVE_DIR" -name "*.md" -type f -mmin +60 2>/dev/null || :)
}

process_dispatches() {
    local count=0
    _cleanup_stuck_dispatches

    for dispatch in "$PENDING_DIR"/*.md; do
        [ -f "$dispatch" ] || continue
        local agent_role
        agent_role=$(extract_agent_role "$dispatch")

        validate_dispatch_preconditions "$dispatch" || continue
        gather_dispatch_intelligence "$dispatch" "$agent_role" "$_PD_TRACK" "$_PD_DISPATCH_ID" "$_PD_GATE" || continue
        execute_and_classify_dispatch "$dispatch" "$_PD_TRACK" "$agent_role" "$_PD_INTEL_RESULT" "$_PD_DISPATCH_ID" || continue

        ((count++))
        sleep 1  # Small delay between dispatches
    done

    [ $count -gt 0 ] && log "V8: Processed $count dispatches"
}

# BOOT-3: Fail-closed startup precondition check.
if [[ -z "${VNX_STATE_DIR:-}" ]] || [[ ! -d "$VNX_STATE_DIR" ]]; then
    echo "FATAL: VNX_STATE_DIR is unset or does not exist: '${VNX_STATE_DIR:-}'" >&2
    echo "Source bin/vnx or set VNX_DATA_DIR before starting the dispatcher." >&2
    exit 1
fi
if [[ -z "${VNX_DATA_DIR:-}" ]] || [[ ! -d "$VNX_DATA_DIR" ]]; then
    echo "FATAL: VNX_DATA_DIR is unset or does not exist: '${VNX_DATA_DIR:-}'" >&2
    echo "Source bin/vnx or set VNX_DATA_DIR before starting the dispatcher." >&2
    exit 1
fi

# Main loop
log "Dispatcher V8 MINIMAL ready. Monitoring $PENDING_DIR for dispatches..."
log "V8 Features: Native skills + instruction-only dispatch (~200 tokens vs 1500 in V7) + multi-provider skill format"
log "V8 Maintains: Mode control, model switching, intelligence v7.4, receipt tracking"
log "Track routing: A→T1(%1), B→T2(%2), C→T3(%3)"

if ! get_pane_ids; then
    log_structured_failure "pane_refresh_failed" "Initial pane ID refresh failed" "phase=startup"
fi

while true; do
    if ! get_pane_ids; then
        log_structured_failure "pane_refresh_failed" "Periodic pane ID refresh failed" "phase=loop"
    fi
    process_dispatches
    sleep 2
done
