#!/bin/bash
# dispatch_deliver.sh — tmux delivery, mode control, and delivery orchestration.
# Sourced by dispatcher_v8_minimal.sh.
# Requires: $STATE_DIR, $VNX_DIR, $VNX_DISPATCH_PAYLOAD_DIR set by orchestrator.
# Requires: dispatch_logging.sh and dispatch_lifecycle.sh sourced first.

tmux_send_best_effort() {
    local target_pane="$1"
    shift
    if ! tmux send-keys -t "$target_pane" "$@" 2>/dev/null; then
        log_structured_failure "tmux_send_failed" "tmux send-keys failed (best-effort)" "pane=$target_pane args=$*"
        return 1
    fi
    return 0
}

# Large-payload tmux buffer loading via temp file to avoid silent truncation.
tmux_load_buffer_safe() {
    local content="$1"
    local payload_size=${#content}
    if [ "$payload_size" -gt "$VNX_DISPATCH_MAX_INLINE" ]; then
        mkdir -p "$VNX_DISPATCH_PAYLOAD_DIR"
        local tmpfile="$VNX_DISPATCH_PAYLOAD_DIR/payload_$$.txt"
        printf '%s' "$content" > "$tmpfile"
        log "V8 DELIVERY: Large payload (${payload_size}B > ${VNX_DISPATCH_MAX_INLINE}B), using temp file"
        if tmux load-buffer "$tmpfile"; then rm -f "$tmpfile"; return 0; else rm -f "$tmpfile"; return 1; fi
    else
        printf '%s' "$content" | tmux load-buffer -
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

get_pane_ids() {
    if ! T0_PANE=$(get_pane_id "t0" "$STATE_DIR/panes.json"); then
        T0_PANE=""; log_structured_failure "pane_lookup_failed" "Failed to resolve T0 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T1_PANE=$(get_pane_id "T1" "$STATE_DIR/panes.json"); then
        T1_PANE=""; log_structured_failure "pane_lookup_failed" "Failed to resolve T1 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T2_PANE=$(get_pane_id "T2" "$STATE_DIR/panes.json"); then
        T2_PANE=""; log_structured_failure "pane_lookup_failed" "Failed to resolve T2 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T3_PANE=$(get_pane_id "T3" "$STATE_DIR/panes.json"); then
        T3_PANE=""; log_structured_failure "pane_lookup_failed" "Failed to resolve T3 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    return 0
}

determine_executor() {
    local track="$1" cognition="$2" requires_mcp="${3:-false}"
    if [ "$requires_mcp" = "true" ] && [ "$track" != "C" ]; then
        log "V8 MCP routing: Track $track → T3 (requires MCP)"
        if [ -n "${T3_PANE:-}" ]; then echo "$T3_PANE"; else echo "$(get_pane_id "T3" "$STATE_DIR/panes.json")"; fi
        return 0
    fi
    case "$track" in
        A) echo "${T1_PANE:-$(get_pane_id "T1" "$STATE_DIR/panes.json")}" ;;
        B) echo "${T2_PANE:-$(get_pane_id "T2" "$STATE_DIR/panes.json")}" ;;
        C) echo "${T3_PANE:-$(get_pane_id "T3" "$STATE_DIR/panes.json")}" ;;
        *) echo "${T1_PANE:-$(get_pane_id "T1" "$STATE_DIR/panes.json")}" ;;
    esac
}

# ===== configure_terminal_mode sub-functions =====

# Globals set by mode_pre_check
_CTM_TERMINAL_ID="" _CTM_PROVIDER="" _CTM_MODE="" _CTM_CLEAR_CONTEXT=""
_CTM_REQUIRES_MODEL="" _CTM_REQUIRES_MODEL_STRENGTH="" _CTM_FORCE_NORMAL=""
_CTM_REQUIRES_PROVIDER="" _CTM_REQUIRES_PROVIDER_STRENGTH=""

# mode_pre_check: resolve terminal/provider, extract mode fields, run provider routing check.
# Params: target_pane dispatch_file
mode_pre_check() {
    local target_pane="$1" dispatch_file="$2"
    _CTM_TERMINAL_ID=$(get_terminal_from_pane "$target_pane" "$STATE_DIR/panes.json" 2>/dev/null || echo "UNKNOWN")
    _CTM_PROVIDER=$(get_terminal_provider "$_CTM_TERMINAL_ID")
    _CTM_MODE=$(extract_mode "$dispatch_file")
    _CTM_CLEAR_CONTEXT=$(extract_clear_context "$dispatch_file")
    _CTM_REQUIRES_MODEL=$(extract_requires_model "$dispatch_file")
    _CTM_REQUIRES_MODEL_STRENGTH=$(extract_requires_model_strength "$dispatch_file")

    # Fallback: if dispatch has no Requires-Model but panes.json specifies a
    # model for this terminal, use the panes.json model. This ensures model
    # switches happen when panes.json is updated (e.g. T2 switched to Opus).
    if [[ -z "$_CTM_REQUIRES_MODEL" && -n "$_CTM_TERMINAL_ID" && "$_CTM_TERMINAL_ID" != "UNKNOWN" ]]; then
        local panes_model
        panes_model=$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_DIR/panes.json'))
    print(d.get('$_CTM_TERMINAL_ID', {}).get('model', ''))
except: pass
" 2>/dev/null)
        if [[ -n "$panes_model" && "$panes_model" != "sonnet" ]]; then
            _CTM_REQUIRES_MODEL="$panes_model"
            _CTM_REQUIRES_MODEL_STRENGTH="advisory"
            log "V8 MODE_CONTROL: Model inferred from panes.json: $_CTM_REQUIRES_MODEL (terminal=$_CTM_TERMINAL_ID)"
        fi
    fi
    _CTM_FORCE_NORMAL=$(extract_force_normal_mode "$dispatch_file")
    _CTM_REQUIRES_PROVIDER=$(extract_requires_provider "$dispatch_file")
    _CTM_REQUIRES_PROVIDER_STRENGTH=$(extract_requires_provider_strength "$dispatch_file")

    local routing_event
    if ! routing_event=$(vnx_eval_provider_routing \
            "$_CTM_REQUIRES_PROVIDER" "$_CTM_REQUIRES_PROVIDER_STRENGTH" "$_CTM_PROVIDER" \
            "$_CTM_TERMINAL_ID" "$(basename "$dispatch_file")"); then
        log "V8 PROVIDER_ROUTING: $routing_event"
        log_structured_failure "provider_mismatch_blocked" \
            "Dispatch blocked — required provider mismatch" \
            "requested_provider=$_CTM_REQUIRES_PROVIDER actual_provider=$_CTM_PROVIDER terminal=$_CTM_TERMINAL_ID"
        return 1
    fi
    log "V8 PROVIDER_ROUTING: $routing_event"
    log "V8 MODE_CONTROL: Config - terminal=$_CTM_TERMINAL_ID provider=$_CTM_PROVIDER mode=$_CTM_MODE clear=$_CTM_CLEAR_CONTEXT model=$_CTM_REQUIRES_MODEL model_strength=$_CTM_REQUIRES_MODEL_STRENGTH force=$_CTM_FORCE_NORMAL"
    return 0
}

# _force_normal_mode: cycle Tab/Shift+Tab to reset Claude Code to normal mode.
# Params: target_pane
_force_normal_mode() {
    local target_pane="$1"
    log "V8 MODE_CONTROL: Forcing normal mode first (safety reset)..."
    # Cycle through modes to ensure we're in normal mode.
    # Do NOT use C-c — it can kill the CLI process.
    if ! tmux_send_best_effort "$target_pane" Tab; then log "V8 MODE_CONTROL: best-effort Tab reset failed (continuing)"; fi
    sleep 0.5
    if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then log "V8 MODE_CONTROL: best-effort Shift+Tab reset failed (continuing)"; fi
    sleep 0.5
    if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then log "V8 MODE_CONTROL: best-effort Shift+Tab cycle failed (continuing)"; fi
    sleep 0.5
    if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then log "V8 MODE_CONTROL: best-effort Shift+Tab normalization failed (continuing)"; fi
    sleep 1
}

# _clear_terminal_context: send context reset command and verify prompt readiness.
# Params: target_pane provider
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

# reset_terminal_context: force_normal step + clear_context step.
# Params: target_pane force_normal clear_context provider
reset_terminal_context() {
    local target_pane="$1" force_normal="$2" clear_context="$3" provider="$4"

    if [[ "$force_normal" == "true" && "$provider" == "claude_code" ]]; then
        _force_normal_mode "$target_pane"
    fi

    if [[ "$clear_context" == "true" ]]; then
        _clear_terminal_context "$target_pane" "$provider" || return 1
    fi
    return 0
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

# _activate_non_claude_mode: handle mode activation for non-Claude-Code providers.
# Params: target_pane mode provider
_activate_non_claude_mode() {
    local target_pane="$1" mode="$2" provider="$3"

    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        if [[ "$mode" == "planning" ]]; then
            log "V8 MODE_CONTROL: Codex planning mode via /plan"
            tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
            sleep 0.3
            if ! tmux_send_best_effort "$target_pane" -l "/plan"; then
                log_structured_failure "plan_mode_activation_failed" "Failed to send /plan command" "pane=$target_pane provider=$provider"; return 1
            fi
            sleep 1
            if ! tmux_send_best_effort "$target_pane" Enter; then
                log_structured_failure "plan_mode_submit_failed" "Failed to submit /plan command" "pane=$target_pane provider=$provider"; return 1
            fi
            sleep 2
        else
            log "V8 MODE_CONTROL: Codex - skipping unsupported mode: $mode"
        fi
    elif [[ "$provider" == "gemini_cli" || "$provider" == "gemini" ]]; then
        log "V8 MODE_CONTROL: Gemini - no mode toggles available (mode=$mode skipped)"
    else
        log "V8 MODE_CONTROL: Unknown provider '$provider' - skipping mode: $mode"
    fi
}

# _activate_claude_planning: switch to Opus and toggle plan mode for Claude Code.
# Params: target_pane
_activate_claude_planning() {
    local target_pane="$1"

    log "V8 MODE_CONTROL: Activating PLANNING mode with Opus model..."
    log "V8: Switching to Opus model for planning mode"
    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.3
    if ! tmux_send_best_effort "$target_pane" -l "/model opus"; then
        log_structured_failure "planning_model_switch_failed" "Failed to switch to Opus for planning mode" "pane=$target_pane"; return 1
    fi
    sleep 1
    if ! tmux_send_best_effort "$target_pane" Enter; then
        log_structured_failure "planning_model_submit_failed" "Failed to submit Opus switch for planning mode" "pane=$target_pane"; return 1
    fi
    sleep 4
    log "V8 MODE_CONTROL: Activating PLAN mode..."
    if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
        log_structured_failure "planning_mode_toggle_failed" "Failed first Shift+Tab for planning mode" "pane=$target_pane"; return 1
    fi
    sleep 0.5
    if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
        log_structured_failure "planning_mode_toggle_failed" "Failed second Shift+Tab for planning mode" "pane=$target_pane"; return 1
    fi
    sleep 2
    log "V8 MODE_CONTROL: Plan mode activated"
}

# activate_terminal_mode: provider-specific mode handling (planning/thinking/normal).
# Params: target_pane mode provider
activate_terminal_mode() {
    local target_pane="$1" mode="$2" provider="$3"

    if [[ "$provider" != "claude_code" ]]; then
        _activate_non_claude_mode "$target_pane" "$mode" "$provider" || return 1
        log "V8 MODE_CONTROL: Configuration complete"
        return 0
    fi

    case "$mode" in
        planning) _activate_claude_planning "$target_pane" || return 1 ;;
        thinking)
            log "V8 MODE_CONTROL: Activating THINKING mode..."
            if ! tmux_send_best_effort "$target_pane" Tab; then
                log_structured_failure "thinking_mode_toggle_failed" "Failed Tab toggle for thinking mode" "pane=$target_pane"; return 1
            fi
            sleep 2
            log "V8 MODE_CONTROL: Thinking mode activated"
            ;;
        none|normal) log "V8 MODE_CONTROL: Staying in NORMAL mode" ;;
        *) log "V8 MODE_CONTROL: Unknown mode: $mode (ignoring)" ;;
    esac

    log "V8 MODE_CONTROL: Configuration complete"
    return 0
}

# configure_terminal_mode — thin wrapper calling the 4 sub-functions in sequence.
configure_terminal_mode() {
    local target_pane="$1" dispatch_file="$2"
    mode_pre_check "$target_pane" "$dispatch_file" || return 1
    reset_terminal_context "$target_pane" "$_CTM_FORCE_NORMAL" "$_CTM_CLEAR_CONTEXT" "$_CTM_PROVIDER" || return 1
    switch_terminal_model "$target_pane" "$_CTM_REQUIRES_MODEL" "$_CTM_REQUIRES_MODEL_STRENGTH" \
        "$_CTM_PROVIDER" "$_CTM_TERMINAL_ID" "$dispatch_file" || return 1
    activate_terminal_mode "$target_pane" "$_CTM_MODE" "$_CTM_PROVIDER" || return 1
    return 0
}

# _ddt_pre_delivery_checks — input-mode guard and worktree resolution.
# Modifies complete_prompt (nameref) with worktree prefix if needed.
# Returns 1 if input mode is blocked (releases lease+claim).
_ddt_pre_delivery_checks() {
    local target_pane="$1" terminal_id="$2" dispatch_id="$3" provider="$4"
    local -n _prompt_ref="$5"

    if ! check_pane_input_ready "$target_pane" "$terminal_id" "$dispatch_id" "$provider"; then
        log "V8 INPUT_MODE: delivery blocked — unrecoverable pane mode terminal=$terminal_id dispatch=$dispatch_id"
        emit_blocked_dispatch_audit "$dispatch_id" "$terminal_id" "blocked_input_mode" "dispatch_blocked" "post_input_mode_blocked"
        rc_release_on_failure "$dispatch_id" "$_DL_RC_ATTEMPT_ID" "$terminal_id" "$_DL_RC_GENERATION" "delivery_failed:post_input_mode_blocked"
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after input mode block" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        return 1
    fi

    local worktree_path
    worktree_path=$(python3 "$VNX_DIR/scripts/terminal_state_shadow.py" get-worktree "$terminal_id" 2>/dev/null || true)
    worktree_path="${worktree_path:-$PROJECT_ROOT}"

    if [ "$worktree_path" != "$PROJECT_ROOT" ] && [ -n "$worktree_path" ]; then
        _prompt_ref="Working-Directory: ${worktree_path}
${_prompt_ref}"
        log "V8 WORKTREE: terminal=$terminal_id path=$worktree_path"
        if [[ "$provider" != "codex_cli" && "$provider" != "codex" ]]; then
            if ! tmux_send_best_effort "$target_pane" "cd '${worktree_path}'" Enter; then
                log "V8 WARNING: Failed to cd to worktree (non-fatal)"
            fi
            sleep 0.3
        fi
    fi
}

# _ddt_send_content — load and paste prompt content via tmux with retry.
# Sets _DDT_FAILED_SUBSTEP on failure.
# Returns 0 on success, 1 on failure.
_DDT_FAILED_SUBSTEP=""
_ddt_send_content() {
    local target_pane="$1" provider="$2" skill_command="$3" complete_prompt="$4"
    _DDT_FAILED_SUBSTEP=""

    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        if ! tmux_retry 3 tmux_load_buffer_safe "${skill_command}${complete_prompt}"; then
            _DDT_FAILED_SUBSTEP="load_buffer"; log "V8 ERROR: Failed to load prompt to tmux buffer (3 attempts)"; return 1
        fi
        if ! tmux_retry 3 tmux paste-buffer -t "$target_pane"; then
            _DDT_FAILED_SUBSTEP="paste_buffer"; log "V8 ERROR: Failed to paste prompt to terminal $target_pane"; return 1
        fi
    else
        if ! tmux_retry 3 tmux_send_best_effort "$target_pane" -l "$skill_command"; then
            _DDT_FAILED_SUBSTEP="send_skill"; log "V8 ERROR: Failed to send skill command to terminal $target_pane"; return 1
        fi
        sleep 0.5
        if ! tmux_retry 3 tmux_load_buffer_safe "$complete_prompt"; then
            _DDT_FAILED_SUBSTEP="load_buffer"; log "V8 ERROR: Failed to load prompt to tmux buffer (3 attempts)"; return 1
        fi
        if ! tmux_retry 3 tmux paste-buffer -t "$target_pane"; then
            _DDT_FAILED_SUBSTEP="paste_buffer"; log "V8 ERROR: Failed to paste prompt to terminal $target_pane"; return 1
        fi
    fi
    return 0
}

# _ddt_handle_failure — log failure, annotate dispatch file, release lease+claim.
_ddt_handle_failure() {
    local dispatch_file="$1" dispatch_id="$2" terminal_id="$3" provider="$4" failed_substep="$5"

    local _failure_code="tx_${failed_substep}"
    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        case "$failed_substep" in
            load_buffer) _failure_code="tx_load_buffer_codex" ;;
            paste_buffer) _failure_code="tx_paste_buffer_codex" ;;
        esac
    fi
    log_structured_failure "delivery_substep_failed" "Delivery substep failed" \
        "substep=$failed_substep dispatch=$dispatch_id terminal=$terminal_id" \
        "$_failure_code" "$dispatch_id" "$terminal_id" "$provider"
    printf '\n\n[DELIVERY_SUBSTEP_FAILED: code=%s] tmux delivery failed at substep. Retry is automatic.\n' \
        "$_failure_code" >> "$dispatch_file"
    rc_release_on_failure "$dispatch_id" "$_DL_RC_ATTEMPT_ID" "$terminal_id" "$_DL_RC_GENERATION" "delivery_failed:$_failure_code"
    if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
        log_structured_failure "claim_release_failed" "Failed to release claim after delivery failure" "terminal=$terminal_id dispatch=$dispatch_id"
    fi
}

# _ddt_subprocess_delivery — route dispatch via SubprocessAdapter instead of tmux.
# Params: terminal_id dispatch_id complete_prompt model dispatch_file
_ddt_subprocess_delivery() {
    local terminal_id="$1" dispatch_id="$2" complete_prompt="$3" model="$4" dispatch_file="$5"

    log "V8 DISPATCH: subprocess adapter route — terminal=$terminal_id dispatch=$dispatch_id model=$model"

    if ! python3 "$VNX_DIR/scripts/lib/subprocess_dispatch.py" \
            --terminal-id "$terminal_id" \
            --instruction "$complete_prompt" \
            --model "$model" \
            --dispatch-id "$dispatch_id"; then
        log_structured_failure "subprocess_delivery_failed" \
            "SubprocessAdapter delivery failed" \
            "terminal=$terminal_id dispatch=$dispatch_id"
        printf '\n\n[DELIVERY_SUBSTEP_FAILED: code=subprocess_delivery_failed] subprocess delivery failed.\n' \
            >> "$dispatch_file"
        rc_release_on_failure "$dispatch_id" "$_DL_RC_ATTEMPT_ID" "$terminal_id" "$_DL_RC_GENERATION" "delivery_failed:subprocess_delivery_failed"
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after subprocess delivery failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        return 1
    fi

    log "V8 DISPATCH: subprocess delivery succeeded — terminal=$terminal_id dispatch=$dispatch_id"
    return 0
}

# deliver_dispatch_to_terminal — input-mode guard, worktree path resolution, delivery.
# Checks VNX_ADAPTER_T{n} env var: if "subprocess", routes via SubprocessAdapter.
# Default (tmux or unset): uses existing tmux send-keys delivery.
# Params: dispatch_file track agent_role dispatch_id target_pane terminal_id
#         provider complete_prompt skill_command
# Reads globals: _DL_RC_GENERATION _DL_RC_ATTEMPT_ID
deliver_dispatch_to_terminal() {
    local dispatch_file="$1" track="$2" agent_role="$3" dispatch_id="$4"
    local target_pane="$5" terminal_id="$6" provider="$7"
    local complete_prompt="$8" skill_command="$9"

    # Resolve per-terminal adapter: VNX_ADAPTER_T0, VNX_ADAPTER_T1, VNX_ADAPTER_T2, etc.
    # T0: set VNX_ADAPTER_T0=subprocess to route T0 via SubprocessAdapter (not default).
    # T1 defaults to subprocess (headless backend-developer) since F32.
    local adapter_var="VNX_ADAPTER_${terminal_id}"
    local adapter_type="${!adapter_var:-tmux}"
    if [[ "$terminal_id" == "T1" && "$adapter_type" == "tmux" && -z "${!adapter_var:-}" ]]; then
        adapter_type="subprocess"
    fi

    if [[ "$adapter_type" == "subprocess" ]]; then
        local model="${_CTM_REQUIRES_MODEL:-sonnet}"
        _ddt_subprocess_delivery "$terminal_id" "$dispatch_id" "$complete_prompt" "$model" "$dispatch_file"
        return $?
    fi

    # Default: tmux delivery path
    _ddt_pre_delivery_checks "$target_pane" "$terminal_id" "$dispatch_id" "$provider" complete_prompt || return 1

    log "V8 DISPATCH: Activating skill '${skill_command}' + pasting instruction"

    if ! _ddt_send_content "$target_pane" "$provider" "$skill_command" "$complete_prompt"; then
        _ddt_handle_failure "$dispatch_file" "$dispatch_id" "$terminal_id" "$provider" "$_DDT_FAILED_SUBSTEP"
        return 1
    fi

    sleep 1  # Allow content to fully paste and render before Enter

    if ! tmux_retry 3 tmux send-keys -t "$target_pane" Enter; then
        _ddt_handle_failure "$dispatch_file" "$dispatch_id" "$terminal_id" "$provider" "enter"
        log "V8 ERROR: Failed to send Enter to terminal $target_pane"
        return 1
    fi

    return 0
}
