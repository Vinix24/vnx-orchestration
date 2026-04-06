#!/usr/bin/env bash

# Pure metadata extractors for dispatch files.

vnx_dispatch_extract_track() {
    local file="$1"
    sed -n 's/^\[\[TARGET:\([A-C]\)\]\].*/\1/p' "$file" | head -1
}

vnx_dispatch_extract_cognition() {
    local file="$1"
    local cognition
    cognition=$(sed -n 's/^Cognition:[[:space:]]*//Ip' "$file" | tr -d ' ')

    if [ -z "$cognition" ]; then
        echo "normal"
    else
        echo "$cognition" | tr '[:upper:]' '[:lower:]'
    fi
}

vnx_dispatch_extract_priority() {
    local file="$1"
    local priority
    priority=$(sed -n 's/^Priority:[[:space:]]*\([^;]*\).*/\1/Ip' "$file" | tr -d ' ')

    if [ -z "$priority" ]; then
        echo "P1"
    else
        echo "$priority"
    fi
}

vnx_dispatch_extract_agent_role() {
    local file="$1"
    local role
    role=$(sed -n 's/^Role:[ ]*//Ip' "$file" | sed 's/[ ]*$//' | xargs)
    role=$(echo "$role" | awk '{print $1}' | xargs)
    echo "$role"
}

vnx_dispatch_normalize_role() {
    local role="$1"
    echo "$role" | tr -d '[:space:][:punct:]' | tr '[:upper:]' '[:lower:]'
}

vnx_dispatch_extract_phase() {
    local file="$1"
    sed -n 's/^Phase:[[:space:]]*//Ip' "$file" | tr -d ' '
}

vnx_dispatch_extract_new_gate() {
    local file="$1"
    sed -n 's/^Gate:[[:space:]]*//Ip' "$file" | tr -d ' '
}

vnx_dispatch_extract_task_id() {
    local file="$1"
    local track="$2"
    local filename
    filename=$(basename "$file" .md)

    if [[ "$filename" =~ ^[A-C][0-9]-[0-9]_ ]]; then
        echo "$filename"
    else
        local phase
        phase=$(vnx_dispatch_extract_phase "$file")
        if [ -n "$phase" ]; then
            echo "${track}${phase}_task"
        else
            echo "${track}_task_$(date +%s)"
        fi
    fi
}

vnx_dispatch_extract_pr_id() {
    local file="$1"
    sed -n 's/^PR-ID:[[:space:]]*//Ip' "$file" | tr -d ' '
}

vnx_dispatch_extract_dispatch_id() {
    local file="$1"
    local dispatch_id

    dispatch_id=$(sed -n 's/^Dispatch-ID:[[:space:]]*//Ip' "$file" | tr -d ' ' | head -1)
    if [ -n "$dispatch_id" ]; then
        echo "$dispatch_id"
        return 0
    fi

    # Fallback: dispatch filename without extension.
    basename "$file" .md
}

vnx_dispatch_extract_mode() {
    local file="$1"
    local mode
    mode=$(sed -n 's/^Mode:[[:space:]]*//Ip' "$file" 2>/dev/null | sed 's/#.*//' | tr -d ' ' | tr '[:upper:]' '[:lower:]')
    echo "${mode:-none}"
}

vnx_dispatch_extract_clear_context() {
    local file="$1"
    local clear
    clear=$(sed -n 's/^ClearContext:[[:space:]]*//Ip' "$file" 2>/dev/null | sed 's/#.*//' | tr -d ' ' | tr '[:upper:]' '[:lower:]')
    echo "${clear:-true}"
}

vnx_dispatch_extract_force_normal_mode() {
    local file="$1"
    local force
    force=$(sed -n 's/^ForceNormalMode:[[:space:]]*//Ip' "$file" 2>/dev/null | sed 's/#.*//' | tr -d ' ' | tr '[:upper:]' '[:lower:]')
    echo "${force:-false}"
}

vnx_dispatch_extract_requires_model() {
    local file="$1"
    local raw
    raw=$(sed -n 's/^Requires-Model:[[:space:]]*//Ip' "$file" 2>/dev/null \
          | sed 's/#.*//' | xargs 2>/dev/null || true)
    local model
    model=$(echo "$raw" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')
    echo "${model:-}"
}

# Returns "required" when Requires-Model carries the hard-blocker suffix,
# "advisory" otherwise (including when the field is absent).
vnx_dispatch_extract_requires_model_strength() {
    local file="$1"
    local raw
    raw=$(sed -n 's/^Requires-Model:[[:space:]]*//Ip' "$file" 2>/dev/null \
          | sed 's/#.*//' | xargs 2>/dev/null || true)
    local lower
    lower=$(echo "$raw" | tr '[:upper:]' '[:lower:]')
    if [[ "$(echo "$lower" | awk '{print $2}')" == "required" ]]; then
        echo "required"
    else
        echo "advisory"
    fi
}

vnx_dispatch_extract_requires_mcp() {
    local file="$1"
    local mcp
    mcp=$(sed -n 's/^Requires-MCP:[[:space:]]*//Ip' "$file" 2>/dev/null | sed 's/#.*//' | tr -d ' ' | tr '[:upper:]' '[:lower:]')
    echo "${mcp:-false}"
}

vnx_dispatch_extract_requires_provider() {
    local file="$1"
    local raw
    raw=$(sed -n 's/^Requires-Provider:[[:space:]]*//Ip' "$file" 2>/dev/null \
          | sed 's/#.*//' | xargs 2>/dev/null || true)
    # Extract first token only — strips optional 'required' strength suffix
    local provider
    provider=$(echo "$raw" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')
    echo "${provider:-}"
}

# Returns "required" when Requires-Provider carries the hard-blocker suffix,
# "advisory" otherwise (including when the field is absent).
vnx_dispatch_extract_requires_provider_strength() {
    local file="$1"
    local raw
    raw=$(sed -n 's/^Requires-Provider:[[:space:]]*//Ip' "$file" 2>/dev/null \
          | sed 's/#.*//' | xargs 2>/dev/null || true)
    local lower
    lower=$(echo "$raw" | tr '[:upper:]' '[:lower:]')
    # Word-boundary check: second token must be exactly 'required'
    if [[ "$(echo "$lower" | awk '{print $2}')" == "required" ]]; then
        echo "required"
    else
        echo "advisory"
    fi
}

