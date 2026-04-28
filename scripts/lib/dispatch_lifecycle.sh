#!/bin/bash
# dispatch_lifecycle.sh — Lifecycle, lease, and runtime-core functions for dispatcher V8.
# Sourced by dispatcher_v8_minimal.sh.
# Requires: $STATE_DIR, $VNX_DIR set by orchestrator; dispatch_logging.sh sourced first.

# Map track identifier to canonical terminal id.
track_to_terminal() {
    case "$1" in
        A) echo "T1" ;;
        B) echo "T2" ;;
        C) echo "T3" ;;
        *) echo "" ;;
    esac
}

# Check if terminal has an active conflicting claim/lock.
# Delegates state parsing to terminal_state_check.py (extracted Python).
terminal_lock_allows_dispatch() {
    local terminal_id="$1"
    local dispatch_id="$2"
    local state_file="$STATE_DIR/terminal_state.json"

    if [ ! -f "$state_file" ]; then
        return 0
    fi

    local check_output
    set +e
    check_output=$(python3 "$SCRIPT_DIR/lib/terminal_state_check.py" "$state_file" "$terminal_id" "$dispatch_id")
    local rc=$?
    set -e

    if [ $rc -ne 0 ]; then
        log "V8 LOCK: check_failed terminal=$terminal_id dispatch=$dispatch_id rc=$rc"
        emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "legacy_check_failed:rc=$rc" "dispatch_blocked"
        return 1
    fi

    if [[ "$check_output" == BLOCK:* ]]; then
        local _block_reason="${check_output#BLOCK:}"
        log "V8 LOCK: blocked terminal=$terminal_id dispatch=$dispatch_id reason=${_block_reason}"
        # Detect duplicate: active_claim held by same dispatch_id
        if [[ "$_block_reason" == active_claim:* ]]; then
            local _holder="${_block_reason#active_claim:}"
            if [[ "$_holder" == "$dispatch_id" ]]; then
                emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "$_block_reason" "duplicate_delivery_prevented"
            else
                emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "$_block_reason" "dispatch_blocked"
            fi
        else
            emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "$_block_reason" "dispatch_blocked"
        fi
        return 1
    fi

    return 0
}

acquire_terminal_claim() {
    local terminal_id="$1" dispatch_id="$2"
    local now_iso; now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! python3 "$VNX_DIR/scripts/terminal_state_shadow.py" \
        --state-dir "$STATE_DIR" --terminal-id "$terminal_id" --status working \
        --claimed-by "$dispatch_id" --claimed-at "$now_iso" --last-activity "$now_iso" \
        --lease-seconds "${VNX_DISPATCH_LEASE_SECONDS:-600}" >/dev/null 2>&1; then
        log "V8 LOCK: acquire_failed terminal=$terminal_id dispatch=$dispatch_id"; return 1
    fi
    log "V8 LOCK: acquired terminal=$terminal_id dispatch=$dispatch_id"; return 0
}

release_terminal_claim() {
    local terminal_id="$1" dispatch_id="$2"
    local now_iso; now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! python3 "$VNX_DIR/scripts/terminal_state_shadow.py" \
        --state-dir "$STATE_DIR" --terminal-id "$terminal_id" --status idle \
        --last-activity "$now_iso" --clear-claim >/dev/null 2>&1; then
        log "V8 LOCK: release_failed terminal=$terminal_id dispatch=$dispatch_id"; return 1
    fi
    log "V8 LOCK: released terminal=$terminal_id dispatch=$dispatch_id"; return 0
}

# ===== RUNTIME CORE INTEGRATION (PR-5) =====
# All functions are non-fatal: failures are logged but never block dispatch.
# When VNX_RUNTIME_PRIMARY=0, functions return immediately without calling Python.

_rc_enabled() {
    [[ "${VNX_RUNTIME_PRIMARY:-1}" == "1" ]]
}

_rc_python() {
    python3 "$VNX_DIR/scripts/runtime_core_cli.py" "$@" 2>/dev/null
}

# Register dispatch with broker before any terminal delivery.
# Writes bundle.json + prompt.txt to .vnx-data/dispatches/<id>/.
rc_register() {
    local dispatch_id="$1" terminal_id="$2" track="$3" skill_name="$4" gate="$5"
    local prompt_file="${6:-}"
    _rc_enabled || return 0

    local args=(register
        --dispatch-id "$dispatch_id"
        --terminal "$terminal_id"
        --track "$track"
        --skill "$skill_name"
        --gate "$gate"
    )
    if [[ -n "$prompt_file" ]]; then
        args+=(--prompt-file "$prompt_file")
    fi

    if ! _rc_python "${args[@]}" > /dev/null; then
        # BOOT-7: fail-closed — registration failure blocks dispatch before lease acquire
        log_structured_failure "registration_failed" \
            "Dispatch registration failed — blocking delivery" \
            "dispatch=$dispatch_id terminal=$terminal_id"
        return 1
    fi
    log "V8 RUNTIME_CORE: registered dispatch=$dispatch_id terminal=$terminal_id"
}

# Check terminal availability via canonical lease before legacy lock check.
# Outputs BLOCK:<reason> or ALLOW if canonical lease says terminal is busy.
# Fail-closed: Python failure or parse error outputs BLOCK, not ALLOW.
rc_check_terminal() {
    local terminal_id="$1" dispatch_id="$2"
    _rc_enabled || { echo "ALLOW"; return 0; }

    local result
    result=$(_rc_python check-terminal --terminal "$terminal_id" --dispatch-id "$dispatch_id") || {
        log "V8 RUNTIME_CORE: check-terminal python failed terminal=$terminal_id dispatch=$dispatch_id — fail closed"
        echo "BLOCK:canonical_check_error:python_failed"
        return 0
    }

    # Fail-closed: JSON parse failure defaults to blocked (not available)
    local available
    available=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("yes" if d.get("available") else "no")' 2>/dev/null || echo "no")

    if [[ "$available" == "no" ]]; then
        local reason
        reason=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("reason","canonical_lease_conflict"))' 2>/dev/null || echo "canonical_check_parse_error")
        echo "BLOCK:canonical_lease:$reason"
    else
        echo "ALLOW"
    fi
}

# Acquire canonical lease alongside terminal_state_shadow write.
# Returns the lease generation (integer) on stdout for later release.
# Fail-closed: on failure, outputs FAIL and returns 1. Caller must block dispatch.
rc_acquire_lease() {
    local terminal_id="$1" dispatch_id="$2"
    local lease_seconds="${VNX_DISPATCH_LEASE_SECONDS:-600}"
    _rc_enabled || { echo "0"; return 0; }

    local result
    result=$(_rc_python acquire-lease \
        --terminal "$terminal_id" \
        --dispatch-id "$dispatch_id" \
        --lease-seconds "$lease_seconds") || {
        log "V8 RUNTIME_CORE: acquire-lease failed terminal=$terminal_id dispatch=$dispatch_id — fail closed"
        echo "FAIL"
        return 1
    }

    # Fail-closed: JSON parse failure means we cannot confirm lease was acquired
    local generation
    generation=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); g=d.get("generation"); print(g if g is not None else "FAIL")' 2>/dev/null || echo "FAIL")
    if [[ "$generation" == "FAIL" ]]; then
        log "V8 RUNTIME_CORE: acquire-lease result parse failed terminal=$terminal_id — fail closed"
        echo "FAIL"
        return 1
    fi
    log "V8 RUNTIME_CORE: lease acquired terminal=$terminal_id generation=$generation"
    echo "$generation"
}

# Record delivery start (broker: claimed -> delivering). Returns attempt_id.
rc_delivery_start() {
    local dispatch_id="$1" terminal_id="$2"
    _rc_enabled || { echo ""; return 0; }

    local result
    result=$(_rc_python delivery-start \
        --dispatch-id "$dispatch_id" \
        --terminal "$terminal_id") || {
        log "V8 RUNTIME_CORE: delivery-start non-fatal failure dispatch=$dispatch_id"
        echo ""
        return 0
    }

    local attempt_id
    attempt_id=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("attempt_id",""))' 2>/dev/null || echo "")
    echo "$attempt_id"
}

# Record delivery success (broker: delivering -> accepted).
# Idempotent: duplicate acceptance returns noop=true instead of failing.
rc_delivery_success() {
    local dispatch_id="$1" attempt_id="$2"
    _rc_enabled || return 0
    [[ -n "$attempt_id" ]] || return 0

    local result
    if result=$(_rc_python delivery-success \
        --dispatch-id "$dispatch_id" \
        --attempt-id "$attempt_id" 2>/dev/null); then
        # Check if this was a no-op (duplicate acceptance)
        local is_noop
        is_noop=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("noop","false"))' 2>/dev/null || echo "false")
        if [[ "$is_noop" == "True" || "$is_noop" == "true" ]]; then
            log "V8 RUNTIME_CORE: delivery-success idempotent no-op dispatch=$dispatch_id (already accepted/beyond)"
        fi
    else
        # Check if this was a terminal-state rejection vs real error
        local is_rejected
        is_rejected=$(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("noop_rejected","false"))' 2>/dev/null || echo "false")
        if [[ "$is_rejected" == "True" || "$is_rejected" == "true" ]]; then
            log "V8 RUNTIME_CORE: delivery-success rejected dispatch=$dispatch_id (terminal state)"
        else
            # RES-D2: Log structured failure when delivery_success recording fails.
            # Broker will show 'delivering' instead of 'accepted' for this dispatch.
            log_structured_failure "delivery_success_record_failed" \
                "delivery-success recording failed — broker state may show delivering instead of accepted" \
                "dispatch=$dispatch_id attempt=$attempt_id"
        fi
    fi
}

# Release canonical lease (leased -> idle).
# Emits structured audit on success and uses log_structured_failure on error.
rc_release_lease() {
    local terminal_id="$1" generation="$2"
    local dispatch_id="${3:-unknown}"
    _rc_enabled || return 0
    [[ -n "$generation" && "$generation" != "0" ]] || return 0

    if ! _rc_python release-lease \
        --terminal "$terminal_id" \
        --generation "$generation" > /dev/null; then
        log_structured_failure "lease_release_failed" \
            "Canonical lease release failed after delivery" \
            "terminal=$terminal_id dispatch=$dispatch_id generation=$generation"
        emit_lease_cleanup_audit "$dispatch_id" "$terminal_id" \
            "lease_release_failed" "false" "release-lease python invocation failed"
        return 1
    fi
    log "V8 RUNTIME_CORE: lease released terminal=$terminal_id dispatch=$dispatch_id"
    emit_lease_cleanup_audit "$dispatch_id" "$terminal_id" \
        "lease_released_on_failure" "true"
}

# Release canonical lease and record delivery failure atomically.
# Preferred over separate delivery-failure + release-lease calls because
# both operations are performed regardless of individual step failure, and the
# combined result is captured in a single structured audit entry.
# Usage: rc_release_on_failure <dispatch_id> <attempt_id> <terminal_id> <generation> [<reason>]
rc_release_on_failure() {
    local dispatch_id="$1" attempt_id="$2" terminal_id="$3" generation="$4"
    local reason="${5:-delivery failed}"
    _rc_enabled || return 0
    [[ -n "$generation" && "$generation" != "0" ]] || return 0

    local result
    result=$(_rc_python release-on-failure \
        --dispatch-id "$dispatch_id" \
        --attempt-id "$attempt_id" \
        --terminal "$terminal_id" \
        --generation "$generation" \
        --reason "$reason") || {
        log_structured_failure "release_on_failure_cli_failed" \
            "release-on-failure CLI invocation failed — emitting direct lease release" \
            "dispatch=$dispatch_id terminal=$terminal_id"
        # Fall back to direct release-lease so the lease is not stranded
        rc_release_lease "$terminal_id" "$generation" "$dispatch_id"
        return 1
    }

    local lease_released cleanup_complete lease_error
    lease_released=$(echo "$result" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(str(d.get("lease_released","false")).lower())' \
        2>/dev/null || echo "false")
    cleanup_complete=$(echo "$result" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(str(d.get("cleanup_complete","false")).lower())' \
        2>/dev/null || echo "false")
    lease_error=$(echo "$result" | python3 -c \
        'import sys,json; d=json.load(sys.stdin); print(d.get("lease_error","") or "")' \
        2>/dev/null || echo "")

    if [[ "$lease_released" == "true" ]]; then
        log "V8 RUNTIME_CORE: lease released on delivery failure terminal=$terminal_id dispatch=$dispatch_id"
        emit_lease_cleanup_audit "$dispatch_id" "$terminal_id" \
            "lease_released_on_failure" "true"
    else
        log_structured_failure "lease_release_failed" \
            "Canonical lease not released after delivery failure" \
            "dispatch=$dispatch_id terminal=$terminal_id error=${lease_error}"
        emit_lease_cleanup_audit "$dispatch_id" "$terminal_id" \
            "lease_release_failed" "false" "$lease_error"
    fi
}

# ===== END RUNTIME CORE INTEGRATION =====

# _adl_check_canonical_lease — validate terminal availability via canonical lease.
# Returns 1 if blocked, 0 if available.
_adl_check_canonical_lease() {
    local terminal_id="$1" dispatch_id="$2"

    local _rc_canonical_check
    _rc_canonical_check=$(rc_check_terminal "$terminal_id" "$dispatch_id")
    if [[ "$_rc_canonical_check" != BLOCK:* ]]; then
        return 0
    fi

    local _rc_block_reason="${_rc_canonical_check#BLOCK:}"
    log "V8 LOCK: canonical_lease blocked terminal=$terminal_id dispatch=$dispatch_id reason=${_rc_block_reason}"
    if [[ "$_rc_block_reason" == canonical_lease:leased:* ]]; then
        local _current_holder="${_rc_block_reason#canonical_lease:leased:}"
        if [[ "$_current_holder" == "$dispatch_id" ]]; then
            emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "$_rc_block_reason" "duplicate_delivery_prevented"
            return 1
        fi
    fi
    emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "$_rc_block_reason" "dispatch_blocked"
    return 1
}

# _adl_register_and_acquire — RC registration + canonical lease acquire.
# Sets _DL_RC_GENERATION and _DL_RC_ATTEMPT_ID. Returns 1 on failure (releases claim).
_adl_register_and_acquire() {
    local dispatch_id="$1" terminal_id="$2" track="$3" skill_name="$4"
    local gate="$5" complete_prompt="$6"

    # BOOT-6/7: registration must precede lease acquire (FK constraint, fail-closed)
    if _rc_enabled; then
        local _rc_prompt_tmpfile="$VNX_DISPATCH_PAYLOAD_DIR/rc_prompt_${dispatch_id}.txt"
        mkdir -p "$VNX_DISPATCH_PAYLOAD_DIR"
        printf '%s' "$complete_prompt" > "$_rc_prompt_tmpfile"
        if ! rc_register "$dispatch_id" "$terminal_id" "$track" "$skill_name" "$gate" "$_rc_prompt_tmpfile"; then
            rm -f "$_rc_prompt_tmpfile"
            log "V8 RUNTIME_CORE: registration blocked dispatch — releasing claim terminal=$terminal_id dispatch=$dispatch_id"
            if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
                log_structured_failure "claim_release_failed" "Failed to release claim after registration failure" "terminal=$terminal_id dispatch=$dispatch_id"
            fi
            return 1
        fi
        rm -f "$_rc_prompt_tmpfile"
    fi

    local _rc_acquire_rc=0
    _DL_RC_GENERATION=$(rc_acquire_lease "$terminal_id" "$dispatch_id") || _rc_acquire_rc=$?
    if [[ "$_rc_acquire_rc" -ne 0 || "$_DL_RC_GENERATION" == "FAIL" ]]; then
        log "V8 LOCK: canonical lease acquire failed — blocking dispatch terminal=$terminal_id dispatch=$dispatch_id"
        emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "canonical_lease_acquire_failed" "dispatch_blocked"
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after lease acquire failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        return 1
    fi

    if _rc_enabled; then
        _DL_RC_ATTEMPT_ID=$(rc_delivery_start "$dispatch_id" "$terminal_id")
        if [[ -z "$_DL_RC_ATTEMPT_ID" ]]; then
            log_structured_failure "delivery_start_no_attempt" \
                "delivery_start returned empty attempt_id — broker failure record will be lost" \
                "dispatch=$dispatch_id terminal=$terminal_id"
        fi
    fi
}

# acquire_dispatch_lease — canonical check, claim, RC register, canonical acquire.
# Params: dispatch_file track terminal_id dispatch_id skill_name gate complete_prompt
# Sets globals: _DL_RC_GENERATION _DL_RC_ATTEMPT_ID. Returns 1 if any step blocks.
_DL_RC_GENERATION="" _DL_RC_ATTEMPT_ID=""

acquire_dispatch_lease() {
    local dispatch_file="$1" track="$2" terminal_id="$3" dispatch_id="$4"
    local skill_name="$5" gate="$6" complete_prompt="$7"
    _DL_RC_GENERATION="" _DL_RC_ATTEMPT_ID=""

    _adl_check_canonical_lease "$terminal_id" "$dispatch_id" || return 1
    terminal_lock_allows_dispatch "$terminal_id" "$dispatch_id" || return 1
    acquire_terminal_claim "$terminal_id" "$dispatch_id" || return 1
    _adl_register_and_acquire "$dispatch_id" "$terminal_id" "$track" "$skill_name" "$gate" "$complete_prompt" || return 1

    return 0
}

# _fdd_update_progress_state — update progress_state.yaml for the track (non-fatal).
_fdd_update_progress_state() {
    local track="$1" gate="$2" dispatch_id="$3"

    if [ ! -f "$VNX_DIR/scripts/update_progress_state.py" ]; then
        log "V8 PROGRESS_STATE: update_progress_state.py not found (non-fatal)"
        return 0
    fi
    log "V8 PROGRESS_STATE: Updating Track $track gate=$gate, status=working, dispatch_id=$dispatch_id"
    if python3 "$VNX_DIR/scripts/update_progress_state.py" \
        --track "$track" --gate "$gate" --status working \
        --dispatch-id "$dispatch_id" --updated-by dispatcher 2>&1; then
        log "V8 PROGRESS_STATE: Successfully updated progress_state.yaml for Track $track"
    else
        log "V8 PROGRESS_STATE: Failed to update progress_state.yaml (non-fatal)"
    fi
}

# _fdd_log_dispatch_metadata — record dispatch metadata to quality_intelligence.db (non-fatal).
_fdd_log_dispatch_metadata() {
    local dispatch_file="$1" dispatch_id="$2" terminal_id="$3" track="$4"
    local agent_role="$5" gate="$6" pr_id="$7" instruction_content="$8"
    local intelligence_data="$9"

    local _dm_cognition _dm_priority _dm_pattern_count _dm_rule_count _dm_instr_chars
    _dm_cognition=$(vnx_dispatch_extract_cognition "$dispatch_file" 2>/dev/null || echo "normal")
    _dm_priority=$(vnx_dispatch_extract_priority "$dispatch_file" 2>/dev/null || echo "P1")
    _dm_pattern_count=$(echo "$intelligence_data" | grep -o '"pattern_count":[0-9]*' | grep -o '[0-9]*' || echo "0")
    _dm_rule_count=$(echo "$intelligence_data" | grep -o '"prevention_rule_count":[0-9]*' | grep -o '[0-9]*' || echo "0")
    _dm_instr_chars=${#instruction_content}
    local _dm_target_oi=""
    _dm_target_oi=$(echo "$instruction_content" | grep -oE 'OI-[0-9]{3,}' | sort -u | paste -sd ',' - 2>/dev/null || echo "")
    python3 "$VNX_DIR/scripts/log_dispatch_metadata.py" \
        --dispatch-id "$dispatch_id" --terminal "$terminal_id" --track "$track" \
        --role "$agent_role" --skill-name "$agent_role" --gate "$gate" \
        --cognition "$_dm_cognition" --priority "$_dm_priority" --pr-id "${pr_id:-}" \
        --pattern-count "${_dm_pattern_count:-0}" --prevention-rule-count "${_dm_rule_count:-0}" \
        --intelligence-json "$intelligence_data" --instruction-char-count "${_dm_instr_chars:-0}" \
        --target-open-items "${_dm_target_oi:-}" 2>/dev/null || {
        log "V8 WARNING: Failed to log dispatch metadata (non-fatal)"
    }
}

# finalize_dispatch_delivery — broker success, progress state, heartbeat, metadata, move to active.
# Params: dispatch_file track terminal_id dispatch_id pr_id gate agent_role instruction_content [intelligence_data]
# Reads globals: _DL_RC_ATTEMPT_ID
finalize_dispatch_delivery() {
    local dispatch_file="$1" track="$2" terminal_id="$3" dispatch_id="$4"
    local pr_id="$5" gate="$6" agent_role="$7" instruction_content="$8"
    local intelligence_data="${9:-}"

    rc_delivery_success "$dispatch_id" "$_DL_RC_ATTEMPT_ID"
    log "V8 DISPATCH: Successfully sent dispatch to terminal $terminal_id"

    _fdd_update_progress_state "$track" "$gate" "$dispatch_id"

    python3 "$VNX_DIR/scripts/notify_dispatch.py" "$dispatch_id" "$terminal_id" "$dispatch_id" "$pr_id" 2>/dev/null || {
        log "V8 WARNING: Failed to notify heartbeat ACK monitor (non-fatal)"
    }

    _fdd_log_dispatch_metadata "$dispatch_file" "$dispatch_id" "$terminal_id" "$track" \
        "$agent_role" "$gate" "$pr_id" "$instruction_content" "$intelligence_data"

    local filename; filename=$(basename "$dispatch_file")
    mv "$dispatch_file" "$ACTIVE_DIR/$filename"
    log "V8 DISPATCH: Activated - moved to $ACTIVE_DIR/$filename"

    # Register dispatch_promoted event (best-effort)
    python3 "$VNX_DIR/scripts/lib/dispatch_register.py" append dispatch_promoted \
        dispatch_id="$dispatch_id" \
        terminal="${terminal_id:-}" \
        >/dev/null 2>&1 || true

    # Throttled non-blocking rebuild via Python helper (single source of truth for throttle+Popen)
    python3 -c "
import sys
sys.path.insert(0, '$VNX_DIR/scripts/lib')
from state_rebuild_trigger import maybe_trigger_state_rebuild
maybe_trigger_state_rebuild()
" >/dev/null 2>&1 || true

    return 0
}
