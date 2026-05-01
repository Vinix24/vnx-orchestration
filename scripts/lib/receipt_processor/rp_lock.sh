# shellcheck shell=bash
# rp_lock.sh - Receipt write lock (flock-based)
# Sourced by scripts/receipt_processor_v4.sh
# Requires: log() from rp_logging.sh, $STATE_DIR

# File descriptor for receipt write lock (flock-based, OS-level atomic)
RECEIPT_LOCK_FD=9
RECEIPT_LOCK_FILE="$STATE_DIR/receipt_write.lock"

# Acquire exclusive lock for receipt writing via flock (prevents race conditions)
acquire_receipt_lock() {
    exec 9>"$RECEIPT_LOCK_FILE"
    if ! flock -w 5 $RECEIPT_LOCK_FD; then
        log "ERROR" "Receipt write lock acquisition failed after 5s (held by another process)"
        return 1
    fi
}

# Release receipt write lock
release_receipt_lock() {
    flock -u $RECEIPT_LOCK_FD 2>/dev/null || true
}
