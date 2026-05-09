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

LEDGER_PATH="${1:?ledger_path required}"
LOCK_PATH="${2:?lock_path required}"
SIZE_THRESHOLD_BYTES="${3:-104857600}"

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
