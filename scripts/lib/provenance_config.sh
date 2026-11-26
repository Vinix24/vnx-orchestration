#!/usr/bin/env bash
# Shared by CI, the hook installer and commit-msg. Only a complete 0/1 pair is usable.
vnx_read_provenance_config() {
    local config
    config="$(python3 "$1/scripts/lib/trace_token_validator.py" config)" || config=""
    case "$config" in
        $'0\n'[01]|$'1\n'[01])
            ENFORCEMENT="${config%%$'\n'*}"
            LEGACY_ACCEPTED="${config#*$'\n'}"
            ;;
        *)
            echo "[VNX] Config read failed: retaining enforcement; requiring Dispatch-ID" >&2
            ENFORCEMENT=1
            LEGACY_ACCEPTED=0
            ;;
    esac
}
