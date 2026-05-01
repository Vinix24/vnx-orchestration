# shellcheck shell=bash
# rp_logging.sh - Logging helpers for receipt_processor_v4.sh
# Sourced by scripts/receipt_processor_v4.sh
# Requires globals: PROCESSING_LOG

# Logging with levels
log() {
    local level="${1:-INFO}"
    shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" | tee -a "$PROCESSING_LOG" >&2
}

log_structured_failure() {
    local code="$1"
    local message="$2"
    local details="${3:-}"
    local payload
    payload="$(python3 - "$code" "$message" "$details" <<'PY'
import json
import sys

code, message, details = sys.argv[1], sys.argv[2], sys.argv[3]
event = {
    "event": "failure",
    "component": "receipt_processor_v4.sh",
    "code": code,
    "message": message,
}
if details:
    event["details"] = details
print(json.dumps(event, separators=(",", ":")))
PY
)"
    log "ERROR" "$payload"
}
