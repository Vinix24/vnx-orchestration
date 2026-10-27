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
