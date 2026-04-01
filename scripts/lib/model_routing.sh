#!/usr/bin/env bash

# Model routing verification — pure logic, no tmux or dispatcher dependencies.
# Sourced by dispatcher_v8_minimal.sh and directly testable in isolation.
#
# Implements the model switch result states defined in:
#   docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md §5
#
# Result states:
#   not_requested   — no Requires-Model field on the dispatch
#   needs_switch    — provider supports /model command; caller must attempt the switch
#   unsupported     — provider does not support runtime model switching (e.g. gemini_cli)
#   switched        — switch command sent and post-switch pane confirms new model is active
#   already_active  — requested model was already active (no switch needed)
#   failed          — switch command sent but pane confirms old model is still active
#   unverified      — switch command sent but pane output does not confirm the result

# ---------------------------------------------------------------------------
# vnx_eval_model_routing — pre-switch evaluation
# ---------------------------------------------------------------------------
#
# Evaluate whether a model switch is supported before attempting it.
# Covers: not_requested, unsupported, and needs_switch cases only.
# The switched/already_active/unverified states are resolved after the switch
# attempt by vnx_verify_model_switch_output + vnx_emit_model_switch_result.
#
# Prints a single JSON coordination event to stdout.
# Returns 0 when the switch may be attempted or is not needed.
# Returns 1 when the switch is unsupported and strength is required.
#
# Arguments:
#   $1 — required_model : model from Requires-Model field (normalized lowercase, may be empty)
#   $2 — strength       : "required" or "advisory"
#   $3 — provider       : provider the terminal is running (normalized lowercase)
#   $4 — terminal_id    : terminal identifier (e.g. T2)
#   $5 — dispatch_id    : dispatch identifier
#
vnx_eval_model_routing() {
    local required_model="$1"
    local strength="$2"
    local provider="$3"
    local terminal_id="$4"
    local dispatch_id="$5"

    # No model requirement on this dispatch — nothing to check
    if [[ -z "$required_model" ]]; then
        printf '{"event":"model_routing","result":"not_requested","terminal":"%s","dispatch":"%s"}\n' \
            "$terminal_id" "$dispatch_id"
        return 0
    fi

    case "$provider" in
        gemini_cli|gemini)
            # Gemini CLI does not support runtime model switching (contract non-goal, §9.3)
            printf '{"event":"model_routing","result":"unsupported","requested_model":"%s","provider":"%s","strength":"%s","terminal":"%s","dispatch":"%s","reason":"gemini_cli does not support runtime model switching"}\n' \
                "$required_model" "$provider" "$strength" "$terminal_id" "$dispatch_id"
            if [[ "$strength" == "required" ]]; then
                return 1
            fi
            return 0
            ;;
        claude_code|codex_cli|codex)
            # Provider supports /model command — caller must perform switch + verify
            printf '{"event":"model_routing","result":"needs_switch","requested_model":"%s","provider":"%s","strength":"%s","terminal":"%s","dispatch":"%s"}\n' \
                "$required_model" "$provider" "$strength" "$terminal_id" "$dispatch_id"
            return 0
            ;;
        *)
            # Unknown provider — switching capability unconfirmed, treat as unsupported
            printf '{"event":"model_routing","result":"unsupported","requested_model":"%s","provider":"%s","strength":"%s","terminal":"%s","dispatch":"%s","reason":"unknown provider, model switching not supported"}\n' \
                "$required_model" "$provider" "$strength" "$terminal_id" "$dispatch_id"
            if [[ "$strength" == "required" ]]; then
                return 1
            fi
            return 0
            ;;
    esac
}

# ---------------------------------------------------------------------------
# vnx_verify_model_switch_output — post-switch pane parser
# ---------------------------------------------------------------------------
#
# Parse captured tmux pane output after a /model command to determine the
# switch result state.
#
# Prints one of: switched | already_active | unverified
# Always returns 0 (result state, not a success/failure signal).
#
# Arguments:
#   $1 — pane_content : captured tmux pane output after the switch delay
#   $2 — model_cmd    : normalized model command token sent (e.g. "default" for opus)
#
vnx_verify_model_switch_output() {
    local pane_content="$1"
    local model_cmd="$2"

    # Build model pattern — "default" may appear as "opus" in Claude Code pane output
    local model_pattern="$model_cmd"
    if [[ "$model_cmd" == "default" ]]; then
        model_pattern="opus|default"
    fi

    # already_active: check before switched to avoid false positive on "Model: opus (already active)"
    if echo "$pane_content" | grep -qiE "already[[:space:]]+(using|on|active|set)"; then
        echo "already_active"
        return 0
    fi

    # switched: model name appears in a model-operation context
    # Claude Code outputs lines like: "Model set to claude-sonnet-4-6"
    #                                 "✓ Model: claude-opus-4-6"
    #                                 "/model sonnet" → "Switched to claude-sonnet-4-6"
    if echo "$pane_content" | grep -qiE \
        "(model[[:space:]]+(set|switched|changed|is|:)[[:space:]].*(${model_pattern})|(${model_pattern})[[:space:]]*(model|is[[:space:]]+active|activated))"; then
        echo "switched"
        return 0
    fi

    # Fallback: the model name appears anywhere in the last 10 lines of pane output
    # (covers shorter confirmation messages and prompt-line model indicators)
    if echo "$pane_content" | tail -10 | grep -qiE "(${model_pattern})"; then
        echo "switched"
        return 0
    fi

    echo "unverified"
    return 0
}

# ---------------------------------------------------------------------------
# vnx_emit_model_switch_result — coordination event + blocking decision
# ---------------------------------------------------------------------------
#
# Emit a model switch result coordination event with full runtime identity fields.
# Returns 0 when the dispatch may proceed; returns 1 when it must be blocked.
#
# Arguments:
#   $1 — requested_model : original model from dispatch (pre-normalization, e.g. "opus")
#   $2 — switch_result   : switched | already_active | unsupported | failed | unverified
#   $3 — actual_model    : detected actual model if known (may be empty)
#   $4 — strength        : "required" or "advisory"
#   $5 — terminal_id     : terminal identifier
#   $6 — dispatch_id     : dispatch identifier
#
vnx_emit_model_switch_result() {
    local requested_model="$1"
    local switch_result="$2"
    local actual_model="$3"
    local strength="$4"
    local terminal_id="$5"
    local dispatch_id="$6"

    local model_match
    local must_block=0

    case "$switch_result" in
        switched|already_active)
            model_match="verified_match"
            ;;
        unsupported|failed|unverified)
            if [[ "$strength" == "required" ]]; then
                model_match="mismatch_blocked"
                must_block=1
            else
                model_match="mismatch_advisory"
            fi
            ;;
        *)
            model_match="unverified"
            if [[ "$strength" == "required" ]]; then
                must_block=1
            fi
            ;;
    esac

    local actual_model_field=""
    if [[ -n "$actual_model" ]]; then
        actual_model_field=',"actual_model":"'"$actual_model"'"'
    fi

    printf '{"event":"model_switch_result","requested_model":"%s"%s,"switch_result":"%s","model_match":"%s","strength":"%s","terminal":"%s","dispatch":"%s"}\n' \
        "$requested_model" "$actual_model_field" "$switch_result" "$model_match" \
        "$strength" "$terminal_id" "$dispatch_id"

    return $must_block
}
