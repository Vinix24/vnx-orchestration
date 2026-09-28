#!/usr/bin/env bash
# Load exactly one variable, DEEPSEEK_API_KEY, from the operator's provider env file.
#
# Sourced by the nightly conversation analyzer. launchd starts it with no shell
# profile, and the profile loader there only keeps `export VNX_` lines, so the
# DeepSeek key (kept in ~/.config/vnx/provider-usage.env) never reached the job.
#
# The file is parsed, never sourced: sourcing would import every variable in it and
# run any command it contains. The value is never printed or logged.
#
# Path: $VNX_PROVIDER_ENV_FILE, default ~/.config/vnx/provider-usage.env.
# Returns 0 when the key was exported, 1 when the file or the key is absent.

vnx_load_deepseek_key() {
    local env_file="${VNX_PROVIDER_ENV_FILE:-$HOME/.config/vnx/provider-usage.env}"
    local line value
    [ -f "$env_file" ] || return 1
    line=$(grep -E '^[[:space:]]*(export[[:space:]]+)?DEEPSEEK_API_KEY=' "$env_file" 2>/dev/null | tail -n 1) || true
    [ -n "$line" ] || return 1
    value="${line#*=}"
    value="${value%$'\r'}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    [ -n "$value" ] || return 1
    export DEEPSEEK_API_KEY="$value"
}
