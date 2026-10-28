#!/bin/bash
# pane_injection.sh — provider-aware prompt injection into a tmux pane.
#
# Moved out of dispatch_deliver.sh (P05): the tmux transport helpers plus the
# provider branch (claude gets the skill command via `send-keys -l` first, codex
# gets skill+prompt pasted as one payload). Names are unchanged —
# dispatch_create.sh calls tmux_send_best_effort directly, the delivery-substep
# contract names tmux_load_buffer_safe, and _ddt_buffer_name carries the OI-1144
# named-buffer rule (one name per delivery, deleted on every exit). The one
# contract crossing the file boundary is PANE_INJECT_FAILED_SUBSTEP, which
# dispatch_deliver.sh maps onto _DDT_FAILED_SUBSTEP.
#
# P06 added the model switch and the context clear; see the section below.
#
# Requires at call time: log() (dispatch_logging.sh) and, for a payload larger
# than VNX_DISPATCH_MAX_INLINE, VNX_DISPATCH_PAYLOAD_DIR (dispatcher_minimal.sh).

# Sourced twice (dispatch_deliver.sh sources it; a caller may source it
# directly). A second source must not redefine the functions below.
if [ -n "${_VNX_PANE_INJECTION_SH_LOADED:-}" ]; then
    return 0
fi
_VNX_PANE_INJECTION_SH_LOADED=1

tmux_send_best_effort() {
    local target_pane="$1"
    shift
    if ! tmux send-keys -t "$target_pane" "$@" 2>/dev/null; then
        log_structured_failure "tmux_send_failed" "tmux send-keys failed (best-effort)" "pane=$target_pane args=$*"
        return 1
    fi
    return 0
}

# OI-1144: unique-per-delivery tmux buffer name. pid + sanitized dispatch_id.
# pid alone already separates two concurrent dispatcher processes racing to
# load+paste (the exact crossing the anonymous buffer stack allowed); the
# dispatch_id keeps the name unique across deliveries within one process and
# greppable in `tmux list-buffers`.
_ddt_buffer_name() {
    local dispatch_id="$1"
    local sanitized="${dispatch_id//[^a-zA-Z0-9_]/_}"
    printf 'vnx-deliver-%s-%s' "$$" "$sanitized"
}

# Large-payload tmux buffer loading via temp file to avoid silent truncation.
# First arg is the NAMED buffer to load into (see _ddt_buffer_name); load+paste
# must address the same named buffer or they fall back to the anonymous stack.
tmux_load_buffer_safe() {
    local buffer_name="$1"
    local content="$2"
    local payload_size=${#content}
    if [ "$payload_size" -gt "$VNX_DISPATCH_MAX_INLINE" ]; then
        mkdir -p "$VNX_DISPATCH_PAYLOAD_DIR"
        local tmpfile="$VNX_DISPATCH_PAYLOAD_DIR/payload_$$.txt"
        printf '%s' "$content" > "$tmpfile"
        log "V8 DELIVERY: Large payload (${payload_size}B > ${VNX_DISPATCH_MAX_INLINE}B), using temp file"
        if tmux load-buffer -b "$buffer_name" "$tmpfile"; then rm -f "$tmpfile"; return 0; else rm -f "$tmpfile"; return 1; fi
    else
        printf '%s' "$content" | tmux load-buffer -b "$buffer_name" -
    fi
}

# Retry wrapper for tmux delivery operations with exponential backoff.
# Usage: tmux_retry <max_attempts> <command...>
tmux_retry() {
    local max_attempts="$1"; shift
    local attempt=1 delay=1
    while [ "$attempt" -le "$max_attempts" ]; do
        if "$@"; then
            [ "$attempt" -gt 1 ] && log "V8 DELIVERY: Succeeded on attempt $attempt"
            return 0
        fi
        if [ "$attempt" -lt "$max_attempts" ]; then
            log "V8 DELIVERY: Attempt $attempt/$max_attempts failed, retrying in ${delay}s..."
            sleep "$delay"
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done
    log "V8 DELIVERY: All $max_attempts attempts failed"
    return 1
}

# pane_inject_prompt — provider-aware prompt injection into one pane.
# Params: target_pane provider skill_command complete_prompt dispatch_id
# On failure sets PANE_INJECT_FAILED_SUBSTEP (send_skill|load_buffer|paste_buffer)
# and returns 1; on success the global is empty and it returns 0. The submitting
# Enter is NOT sent here: the delivery orchestrator sends it as its own keystroke.
PANE_INJECT_FAILED_SUBSTEP=""
pane_inject_prompt() {
    local target_pane="$1" provider="$2" skill_command="$3" complete_prompt="$4" dispatch_id="$5"
    PANE_INJECT_FAILED_SUBSTEP=""

    # OI-1144: one named buffer per delivery, shared by load+paste, deleted on
    # every exit path so the buffer stack never accumulates. Retries re-address
    # the SAME name, so a retry can never pick up another delivery's content.
    local buf_name
    buf_name=$(_ddt_buffer_name "$dispatch_id")

    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        if ! tmux_retry 3 tmux_load_buffer_safe "$buf_name" "${skill_command}${complete_prompt}"; then
            PANE_INJECT_FAILED_SUBSTEP="load_buffer"; log "V8 ERROR: Failed to load prompt to tmux buffer (3 attempts)"; tmux delete-buffer -b "$buf_name" 2>/dev/null || true; return 1
        fi
        if ! tmux_retry 3 tmux paste-buffer -b "$buf_name" -t "$target_pane"; then
            PANE_INJECT_FAILED_SUBSTEP="paste_buffer"; log "V8 ERROR: Failed to paste prompt to terminal $target_pane"; tmux delete-buffer -b "$buf_name" 2>/dev/null || true; return 1
        fi
    else
        if ! tmux_retry 3 tmux_send_best_effort "$target_pane" -l "$skill_command"; then
            PANE_INJECT_FAILED_SUBSTEP="send_skill"; log "V8 ERROR: Failed to send skill command to terminal $target_pane"; tmux delete-buffer -b "$buf_name" 2>/dev/null || true; return 1
        fi
        sleep 0.5
        if ! tmux_retry 3 tmux_load_buffer_safe "$buf_name" "$complete_prompt"; then
            PANE_INJECT_FAILED_SUBSTEP="load_buffer"; log "V8 ERROR: Failed to load prompt to tmux buffer (3 attempts)"; tmux delete-buffer -b "$buf_name" 2>/dev/null || true; return 1
        fi
        if ! tmux_retry 3 tmux paste-buffer -b "$buf_name" -t "$target_pane"; then
            PANE_INJECT_FAILED_SUBSTEP="paste_buffer"; log "V8 ERROR: Failed to paste prompt to terminal $target_pane"; tmux delete-buffer -b "$buf_name" 2>/dev/null || true; return 1
        fi
    fi
    tmux delete-buffer -b "$buf_name" 2>/dev/null || true
    return 0
}

# ===== Model switch and context clear (P06) =====
# Moved out of dispatch_deliver.sh, bodies unchanged: reset_terminal_context
# calls the clear, configure_terminal_mode and dispatch_create.sh's
# _pdp_apply_terminal_mode_setup call the switch. Call-time deps, looked up
# where they are used: log/log_structured_failure, get_context_reset_command,
# the three vnx_* routing helpers (model_routing.sh), python3, capture-pane.

_clear_terminal_context() {
    local target_pane="$1" provider="$2"

    local reset_cmd
    reset_cmd=$(get_context_reset_command "$provider")
    log "V8 MODE_CONTROL: Clearing context via $reset_cmd ..."
    # C-u safely clears readline buffer (C-c would kill CLI process)
    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.3
    if ! tmux_send_best_effort "$target_pane" -l "$reset_cmd"; then
        log_structured_failure "context_reset_failed" "Failed to send context reset command" "pane=$target_pane provider=$provider"
        return 1
    fi
    sleep 1
    if ! tmux_send_best_effort "$target_pane" Enter; then
        log_structured_failure "context_reset_submit_failed" "Failed to submit context reset command" "pane=$target_pane provider=$provider"
        return 1
    fi
    case "$provider" in
        gemini_cli|gemini) sleep 6 ;;
        codex_cli|codex)   sleep 4 ;;
        *)                 sleep 3 ;;
    esac
    local pane_content
    pane_content=$(tmux capture-pane -p -t "$target_pane" 2>/dev/null || true)
    if echo "$pane_content" | grep -qi "Was this conversation helpful"; then
        log "V8 MODE_CONTROL: Feedback modal detected after clear — dismissing with Enter"
        tmux_send_best_effort "$target_pane" Enter 2>/dev/null || true
        sleep 2
        pane_content=$(tmux capture-pane -p -t "$target_pane" 2>/dev/null || true)
    fi
    if ! echo "$pane_content" | grep -qE '(❯|>\s*$|\$\s*$|%\s*$)'; then
        log "V8 MODE_CONTROL: Warning — terminal may not be ready after clear (no prompt detected), adding extra delay"
        sleep 2
    fi
}

# _stm_send_switch_command — send /model command via tmux and verify.
# Params: target_pane model_cmd requires_model requires_model_strength terminal_id dispatch_basename
_stm_send_switch_command() {
    local target_pane="$1" model_cmd="$2" requires_model="$3"
    local requires_model_strength="$4" terminal_id="$5" dispatch_basename="$6"

    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.3

    local switch_send_ok=true
    if ! tmux_send_best_effort "$target_pane" -l "/model $model_cmd"; then switch_send_ok=false; fi
    if [[ "$switch_send_ok" == "true" ]]; then
        sleep 1
        if ! tmux_send_best_effort "$target_pane" Enter; then switch_send_ok=false; fi
    fi

    if [[ "$switch_send_ok" == "false" ]]; then
        local fail_event
        if ! fail_event=$(vnx_emit_model_switch_result \
                "$requires_model" "failed" "" "$requires_model_strength" \
                "$terminal_id" "$dispatch_basename"); then
            log "V8 MODEL_ROUTING: $fail_event"
            log_structured_failure "model_switch_blocked" \
                "Dispatch blocked — required model switch command could not be sent" \
                "requested_model=$requires_model terminal=$terminal_id"
            return 1
        fi
        log "V8 MODEL_ROUTING: $fail_event"
        return 0
    fi

    sleep 4  # Critical delay for model switch to complete
    local pane_content switch_result
    pane_content=$(tmux capture-pane -p -t "$target_pane" 2>/dev/null || true)
    switch_result=$(vnx_verify_model_switch_output "$pane_content" "$model_cmd")
    local result_event
    if ! result_event=$(vnx_emit_model_switch_result \
            "$requires_model" "$switch_result" "" "$requires_model_strength" \
            "$terminal_id" "$dispatch_basename"); then
        log "V8 MODEL_ROUTING: $result_event"
        log_structured_failure "model_switch_blocked" \
            "Dispatch blocked — required model switch could not be verified" \
            "requested_model=$requires_model switch_result=$switch_result terminal=$terminal_id"
        return 1
    fi
    log "V8 MODEL_ROUTING: $result_event"
}

# switch_terminal_model: model routing decision, normalization, send switch command, verify.
# Params: target_pane requires_model requires_model_strength provider terminal_id dispatch_file
switch_terminal_model() {
    local target_pane="$1" requires_model="$2" requires_model_strength="$3"
    local provider="$4" terminal_id="$5" dispatch_file="$6"

    local model_pre_event
    if ! model_pre_event=$(vnx_eval_model_routing \
            "$requires_model" "$requires_model_strength" "$provider" \
            "$terminal_id" "$(basename "$dispatch_file")"); then
        log "V8 MODEL_ROUTING: $model_pre_event"
        log_structured_failure "model_routing_blocked" \
            "Dispatch blocked — required model routing pre-check failed" \
            "requested_model=$requires_model provider=$provider terminal=$terminal_id"
        return 1
    fi
    log "V8 MODEL_ROUTING: $model_pre_event"

    local model_pre_result
    model_pre_result=$(echo "$model_pre_event" | \
        python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result',''))" 2>/dev/null || echo "")

    if [[ "$model_pre_result" == "needs_switch" ]]; then
        local model_cmd="$requires_model"
        # Normalize: "opus" -> "default" selects Opus 4.6 1M context (not 200K)
        [[ "$model_cmd" == "opus" ]] && model_cmd="default"
        log "V8 MODEL_ROUTING: Switching to model: $model_cmd (raw=$requires_model provider=$provider)"
        _stm_send_switch_command "$target_pane" "$model_cmd" "$requires_model" \
            "$requires_model_strength" "$terminal_id" "$(basename "$dispatch_file")" || return 1
    fi
    return 0
}
