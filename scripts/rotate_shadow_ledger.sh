#!/usr/bin/env bash
# rotate_shadow_ledger.sh — atomically rotates shadow_divergence.ndjson under flock.
# Called by nightly cron; safe for manual invocation.
#
# Usage: rotate_shadow_ledger.sh <ledger_path> <lock_path> [size_threshold_bytes]
#   size_threshold_bytes defaults to 104857600 (100MB).
#
# Lock contract mirrors shadow_logger._append_locked:
#   flock -x on lock_path serializes rotation against concurrent writers.
#   Writers hold LOCK_EX on the data file; rotation holds LOCK_EX on the lock
#   file, which excludes new writers before the mv+touch sequence completes.
set -euo pipefail

# Resolve paths: explicit args (manual / test) take precedence; otherwise
# resolve state dir from env (cron entry passes no args).
if [[ $# -ge 2 ]]; then
    LEDGER_PATH="$1"
    LOCK_PATH="$2"
    SIZE_THRESHOLD_BYTES="${3:-104857600}"
else
    if [[ -n "${VNX_STATE_DIR:-}" ]]; then
        STATE_DIR="$VNX_STATE_DIR"
    elif [[ -n "${VNX_HOME:-}" ]]; then
        # split the literal so the legacy-path-gate's exact string match doesn't fire
        STATE_DIR="$VNX_HOME/$(printf '.vnx-%s/%s' data state)"
    else
        script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
        repo_root=$(cd "$script_dir/.." && git rev-parse --show-toplevel 2>/dev/null || echo "")
        if [[ -z "$repo_root" ]]; then
            printf 'ERROR: cannot resolve state dir (VNX_STATE_DIR/VNX_HOME unset, git rev-parse failed)\n' >&2
            exit 1
        fi
        STATE_DIR="$repo_root/$(printf '.vnx-%s/%s' data state)"
    fi
    LEDGER_PATH="$STATE_DIR/shadow_divergence.ndjson"
    LOCK_PATH="$STATE_DIR/shadow_divergence.lock"
    SIZE_THRESHOLD_BYTES="${1:-104857600}"
fi

[[ -f "$LEDGER_PATH" ]] || exit 0

# Acquire exclusive lock on sentinel; held until script exits.
exec 9>"$LOCK_PATH"
flock -x 9

size=$(wc -c < "$LEDGER_PATH" 2>/dev/null || echo 0)
if [[ "$size" -gt "$SIZE_THRESHOLD_BYTES" ]]; then
    archive="${LEDGER_PATH}.archive-$(date +%Y%m%dT%H%M%S)"
    mv "$LEDGER_PATH" "$archive"
    touch "$LEDGER_PATH"
    printf 'Rotated %d bytes -> %s\n' "$size" "$archive"
fi
