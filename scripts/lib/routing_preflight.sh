#!/usr/bin/env bash

# Routing preflight — validate provider and model readiness before dispatch.
# Sourced by kickoff_preflight or usable standalone.
# Implements PR-3 §: Kickoff, Preset, and Preflight Provider Readiness
# Contract: docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md
#
# Compatible with bash 3.2+ (macOS default) — no associative arrays.

# ---------------------------------------------------------------------------
# Terminal configuration defaults (pinned assumptions, contract §5.3)
# ---------------------------------------------------------------------------

# Returns the canonical pinned model for a terminal.
_vnx_pinned_model() {
    case "$1" in
        T0) echo "default" ;;
        T1) echo "sonnet" ;;
        T2) echo "sonnet" ;;
        T3) echo "default" ;;
        *)  echo "default" ;;
    esac
}

# Returns the canonical pinned provider for a terminal.
_vnx_pinned_provider() {
    case "$1" in
        T0|T1|T2|T3) echo "claude_code" ;;
        *)            echo "claude_code" ;;
    esac
}

# Returns 0 if provider supports runtime model switching, 1 otherwise.
_vnx_provider_can_switch_model() {
    case "$1" in
        claude_code|codex_cli|codex) return 0 ;;
        *) return 1 ;;
    esac
}

# Returns 0 if provider is a known/supported provider, 1 otherwise.
_vnx_is_known_provider() {
    case "$1" in
        claude_code|codex_cli|codex|gemini_cli|gemini) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# vnx_resolve_terminal_provider — get effective provider for a terminal
# ---------------------------------------------------------------------------
vnx_resolve_terminal_provider() {
    local terminal_id="$1"
    local env_key="VNX_${terminal_id}_PROVIDER"
    local env_val="${!env_key:-}"
    if [[ -n "$env_val" ]]; then
        echo "$env_val" | tr '[:upper:]' '[:lower:]'
    else
        _vnx_pinned_provider "$terminal_id"
    fi
}

# ---------------------------------------------------------------------------
# vnx_resolve_terminal_model — get effective model for a terminal
# ---------------------------------------------------------------------------
vnx_resolve_terminal_model() {
    local terminal_id="$1"
    local env_key="VNX_${terminal_id}_MODEL"
    local env_val="${!env_key:-}"
    if [[ -n "$env_val" ]]; then
        echo "$env_val" | tr '[:upper:]' '[:lower:]'
    else
        _vnx_pinned_model "$terminal_id"
    fi
}

# ---------------------------------------------------------------------------
# vnx_check_provider_readiness — single terminal provider readiness check
# ---------------------------------------------------------------------------
#
# Prints a JSON diagnostic event. Returns 0 when ready, 1 when blocked.
#
# Arguments:
#   $1 — terminal_id       : T0|T1|T2|T3
#   $2 — required_provider : provider the dispatch needs (may be empty)
#   $3 — strength          : "required" or "advisory"
#
vnx_check_provider_readiness() {
    local terminal_id="$1"
    local required_provider="$2"
    local strength="$3"

    if [[ -z "$required_provider" ]]; then
        printf '{"event":"preflight_provider","result":"not_required","terminal":"%s"}\n' \
            "$terminal_id"
        return 0
    fi

    local actual_provider
    actual_provider=$(vnx_resolve_terminal_provider "$terminal_id")

    if [[ "$required_provider" == "$actual_provider" ]]; then
        printf '{"event":"preflight_provider","result":"ready","terminal":"%s","provider":"%s","strength":"%s"}\n' \
            "$terminal_id" "$actual_provider" "$strength"
        return 0
    fi

    # Mismatch — classify the gap
    local gap_type="unsupported"
    if _vnx_is_known_provider "$required_provider"; then
        gap_type="misconfigured"
    fi

    local diagnostic="Terminal $terminal_id runs $actual_provider but dispatch requires $required_provider ($strength). Gap: $gap_type."

    printf '{"event":"preflight_provider","result":"not_ready","terminal":"%s","required_provider":"%s","actual_provider":"%s","strength":"%s","gap":"%s","diagnostic":"%s"}\n' \
        "$terminal_id" "$required_provider" "$actual_provider" "$strength" "$gap_type" "$diagnostic"

    if [[ "$strength" == "required" ]]; then
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# vnx_check_model_readiness — single terminal model readiness check
# ---------------------------------------------------------------------------
#
# Prints a JSON diagnostic event. Returns 0 when ready, 1 when blocked.
#
# Arguments:
#   $1 — terminal_id     : T0|T1|T2|T3
#   $2 — required_model  : model the dispatch needs (may be empty)
#   $3 — strength        : "required" or "advisory"
#
vnx_check_model_readiness() {
    local terminal_id="$1"
    local required_model="$2"
    local strength="$3"

    if [[ -z "$required_model" ]]; then
        printf '{"event":"preflight_model","result":"not_required","terminal":"%s"}\n' \
            "$terminal_id"
        return 0
    fi

    local actual_model
    actual_model=$(vnx_resolve_terminal_model "$terminal_id")
    local actual_provider
    actual_provider=$(vnx_resolve_terminal_provider "$terminal_id")

    # Normalize: opus == default
    local norm_required="$required_model"
    local norm_actual="$actual_model"
    [[ "$norm_required" == "opus" ]] && norm_required="default"
    [[ "$norm_actual" == "opus" ]] && norm_actual="default"

    if [[ "$norm_required" == "$norm_actual" ]]; then
        printf '{"event":"preflight_model","result":"ready","terminal":"%s","model":"%s","source":"pinned","strength":"%s"}\n' \
            "$terminal_id" "$actual_model" "$strength"
        return 0
    fi

    # Model differs — can the provider switch at runtime?
    if _vnx_provider_can_switch_model "$actual_provider"; then
        printf '{"event":"preflight_model","result":"ready_with_switch","terminal":"%s","pinned_model":"%s","required_model":"%s","provider":"%s","strength":"%s"}\n' \
            "$terminal_id" "$actual_model" "$required_model" "$actual_provider" "$strength"
        return 0
    fi

    # Provider cannot switch models at runtime
    local diagnostic="Terminal $terminal_id is pinned to $actual_model on $actual_provider which does not support runtime model switching. Dispatch requires $required_model ($strength)."

    printf '{"event":"preflight_model","result":"not_ready","terminal":"%s","pinned_model":"%s","required_model":"%s","provider":"%s","strength":"%s","gap":"unsupported","diagnostic":"%s"}\n' \
        "$terminal_id" "$actual_model" "$required_model" "$actual_provider" "$strength" "$diagnostic"

    if [[ "$strength" == "required" ]]; then
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# vnx_check_pinned_assumptions — verify pinned terminal model assumptions
# ---------------------------------------------------------------------------
#
# Contract rule PA-1: pinned assumptions satisfy required constraints only
# when the source is machine-verifiable.
#
# Prints one JSON event per terminal. Returns 0 when all hold, 1 on drift.
#
vnx_check_pinned_assumptions() {
    local drift_found=0

    for tid in T0 T1 T2 T3; do
        local expected_model
        expected_model=$(_vnx_pinned_model "$tid")
        local actual_model
        actual_model=$(vnx_resolve_terminal_model "$tid")
        local expected_provider
        expected_provider=$(_vnx_pinned_provider "$tid")
        local actual_provider
        actual_provider=$(vnx_resolve_terminal_provider "$tid")

        # Normalize opus == default
        local norm_expected="$expected_model"
        local norm_actual="$actual_model"
        [[ "$norm_expected" == "opus" ]] && norm_expected="default"
        [[ "$norm_actual" == "opus" ]] && norm_actual="default"

        local model_ok="true"
        local provider_ok="true"
        [[ "$norm_expected" != "$norm_actual" ]] && model_ok="false"
        [[ "$expected_provider" != "$actual_provider" ]] && provider_ok="false"

        if [[ "$model_ok" == "true" && "$provider_ok" == "true" ]]; then
            printf '{"event":"pinned_assumption","result":"verified","terminal":"%s","provider":"%s","model":"%s"}\n' \
                "$tid" "$actual_provider" "$actual_model"
        else
            drift_found=1
            local diagnostic=""
            [[ "$provider_ok" == "false" ]] && diagnostic="provider: expected=$expected_provider actual=$actual_provider. "
            [[ "$model_ok" == "false" ]] && diagnostic="${diagnostic}model: expected=$expected_model actual=$actual_model."
            printf '{"event":"pinned_assumption","result":"drift","terminal":"%s","expected_provider":"%s","actual_provider":"%s","expected_model":"%s","actual_model":"%s","diagnostic":"%s"}\n' \
                "$tid" "$expected_provider" "$actual_provider" "$expected_model" "$actual_model" "$diagnostic"
        fi
    done

    return $drift_found
}

# ---------------------------------------------------------------------------
# vnx_preflight_routing_readiness — full chain readiness check
# ---------------------------------------------------------------------------
#
# Prints structured JSON diagnostics to stdout.
# Returns 0 when all required checks pass, 1 when any required check fails.
#
# Arguments:
#   $1 — terminal_id        : target terminal (T0|T1|T2|T3)
#   $2 — required_provider  : from dispatch metadata (may be empty)
#   $3 — provider_strength  : "required" or "advisory"
#   $4 — required_model     : from dispatch metadata (may be empty)
#   $5 — model_strength     : "required" or "advisory"
#
vnx_preflight_routing_readiness() {
    local terminal_id="$1"
    local required_provider="$2"
    local provider_strength="$3"
    local required_model="$4"
    local model_strength="$5"

    local any_block=0

    if ! vnx_check_provider_readiness "$terminal_id" "$required_provider" "$provider_strength"; then
        any_block=1
    fi

    if ! vnx_check_model_readiness "$terminal_id" "$required_model" "$model_strength"; then
        any_block=1
    fi

    return $any_block
}

# ---------------------------------------------------------------------------
# vnx_preflight_preset_diagnostics — check if a preset can satisfy routing
# ---------------------------------------------------------------------------
#
# Arguments:
#   $1 — preset_file        : path to .env preset file
#   $2 — terminal_id        : target terminal
#   $3 — required_provider  : from dispatch (may be empty)
#   $4 — required_model     : from dispatch (may be empty)
#
vnx_preflight_preset_diagnostics() {
    local preset_file="$1"
    local terminal_id="$2"
    local required_provider="$3"
    local required_model="$4"

    if [[ ! -f "$preset_file" ]]; then
        printf '{"event":"preflight_preset","result":"error","preset":"%s","diagnostic":"Preset file not found"}\n' \
            "$preset_file"
        return 1
    fi

    local preset_name
    preset_name=$(basename "${preset_file%.env}")

    # Source preset in subshell to avoid polluting current env
    local provider_ok model_ok
    provider_ok=$(
        source "$preset_file" 2>/dev/null
        local p
        p=$(vnx_resolve_terminal_provider "$terminal_id")
        if [[ -z "$required_provider" || "$required_provider" == "$p" ]]; then
            echo "ready"
        else
            echo "not_ready:$p"
        fi
    )
    model_ok=$(
        source "$preset_file" 2>/dev/null
        local m
        m=$(vnx_resolve_terminal_model "$terminal_id")
        local norm_r="$required_model" norm_a="$m"
        [[ "$norm_r" == "opus" ]] && norm_r="default"
        [[ "$norm_a" == "opus" ]] && norm_a="default"
        if [[ -z "$required_model" || "$norm_r" == "$norm_a" ]]; then
            echo "ready"
        else
            echo "not_ready:$m"
        fi
    )

    if [[ "$provider_ok" == "ready" && "$model_ok" == "ready" ]]; then
        printf '{"event":"preflight_preset","result":"ready","preset":"%s","terminal":"%s"}\n' \
            "$preset_name" "$terminal_id"
        return 0
    fi

    local diag_parts=""
    if [[ "$provider_ok" != "ready" ]]; then
        local actual_p="${provider_ok#not_ready:}"
        diag_parts="provider: preset provides $actual_p but $required_provider required"
    fi
    if [[ "$model_ok" != "ready" ]]; then
        local actual_m="${model_ok#not_ready:}"
        [[ -n "$diag_parts" ]] && diag_parts="$diag_parts; "
        diag_parts="${diag_parts}model: preset provides $actual_m but $required_model required"
    fi

    printf '{"event":"preflight_preset","result":"not_ready","preset":"%s","terminal":"%s","diagnostic":"%s"}\n' \
        "$preset_name" "$terminal_id" "$diag_parts"
    return 1
}
