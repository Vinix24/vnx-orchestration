#!/usr/bin/env bash

# Provider routing enforcement — pure logic, no tmux or dispatcher dependencies.
# Sourced by dispatcher_v8_minimal.sh and directly testable in isolation.

# Evaluate a provider routing requirement and return the outcome.
#
# Prints a single JSON coordination event to stdout describing the result.
# Returns 0 when the dispatch may proceed; returns 1 when it must be blocked.
#
# Arguments:
#   $1 — required_provider  : provider from Requires-Provider field (normalized lowercase, may be empty)
#   $2 — strength           : "required" or "advisory"
#   $3 — actual_provider    : provider the terminal is running (normalized lowercase)
#   $4 — terminal_id        : terminal identifier (e.g. T2) — for event context
#   $5 — dispatch_id        : dispatch identifier — for event context
#
vnx_eval_provider_routing() {
    local required_provider="$1"
    local strength="$2"
    local actual_provider="$3"
    local terminal_id="$4"
    local dispatch_id="$5"

    # No provider requirement on this dispatch — nothing to check
    if [[ -z "$required_provider" ]]; then
        printf '{"event":"provider_routing","result":"not_required","terminal":"%s","dispatch":"%s"}\n' \
            "$terminal_id" "$dispatch_id"
        return 0
    fi

    # Provider matches
    if [[ "$required_provider" == "$actual_provider" ]]; then
        printf '{"event":"provider_routing","result":"match","provider":"%s","strength":"%s","terminal":"%s","dispatch":"%s"}\n' \
            "$actual_provider" "$strength" "$terminal_id" "$dispatch_id"
        return 0
    fi

    # Provider mismatch — behavior depends on strength
    if [[ "$strength" == "required" ]]; then
        # Hard blocker: fail closed, return 1 to block dispatch
        printf '{"event":"provider_routing","result":"mismatch_blocked","requested_provider":"%s","actual_provider":"%s","strength":"required","terminal":"%s","dispatch":"%s","reason":"required provider mismatch"}\n' \
            "$required_provider" "$actual_provider" "$terminal_id" "$dispatch_id"
        return 1
    else
        # Advisory: warn and proceed, return 0
        printf '{"event":"provider_routing","result":"mismatch_advisory","requested_provider":"%s","actual_provider":"%s","strength":"advisory","terminal":"%s","dispatch":"%s"}\n' \
            "$required_provider" "$actual_provider" "$terminal_id" "$dispatch_id"
        return 0
    fi
}
