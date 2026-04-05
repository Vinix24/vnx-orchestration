#!/bin/bash
# dispatch_logging.sh — Logging and audit functions for dispatcher V8.
# Sourced by dispatcher_v8_minimal.sh. Requires: $STATE_DIR set by orchestrator.
# Functions: log, log_structured_failure, _classify_blocked_dispatch,
#            emit_blocked_dispatch_audit, emit_lease_cleanup_audit

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Structured failure event logging for shell/Python boundary diagnostics.
# Enhanced per DFL-LOG-1 (Contract 160): emits failure_code, failure_class,
# retryable, operator_summary, dispatch_id, terminal_id, provider, and phase.
#
# Usage: log_structured_failure <code> <message> <details> [<failure_code> <dispatch_id> <terminal_id> <provider>]
log_structured_failure() {
    local code="$1"
    local message="$2"
    local details="${3:-}"
    local failure_code="${4:-}"
    local dispatch_id="${5:-}"
    local terminal_id="${6:-}"
    local provider="${7:-}"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"

    local payload
    payload="$(python3 - "$code" "$message" "$details" "$failure_code" "$dispatch_id" "$terminal_id" "$provider" <<'PY'
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(".")), "scripts", "lib"))
code, message, details, failure_code, dispatch_id, terminal_id, provider = sys.argv[1:8]
event = {
    "event": "delivery_failure",
    "component": "dispatcher_v8_minimal.sh",
    "code": code,
    "message": message,
}
if details:
    event["details"] = details
if failure_code:
    event["failure_code"] = failure_code
    phase = failure_code.split("_")[0] if "_" in failure_code else ""
    event["phase"] = phase
    try:
        sys.path.insert(0, os.path.join(os.environ.get("VNX_HOME", "."), "scripts", "lib"))
        from failure_classifier import FAILURE_CODE_REGISTRY
        entry = FAILURE_CODE_REGISTRY.get(failure_code)
        if entry:
            event["failure_class"] = entry[0]
            event["retryable"] = entry[1]
            event["operator_summary"] = entry[3]
    except Exception:
        pass
if dispatch_id:
    event["dispatch_id"] = dispatch_id
if terminal_id:
    event["terminal_id"] = terminal_id
if provider:
    event["provider"] = provider
print(json.dumps(event, separators=(",", ":")))
PY
)"

    echo "[$ts] $payload"
}

# Classify a block reason into category and requeueable flag.
# Outputs: "<category> <requeueable>" where category is one of:
#   busy      — terminal has a healthy active lease (defer)
#   ambiguous — lease expired or state unreadable (requeue)
#   invalid   — metadata invalid, skill bad, dependency error (reject)
_classify_blocked_dispatch() {
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*)
            echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable)
            echo "ambiguous true" ;;
        canonical_check_parse_error|canonical_lease_acquire_failed)
            # RC-2: canonical_check_parse_error is a transient JSON parse failure
            # (not a metadata defect) — must be ambiguous, not invalid (contract 140 §2.3).
            # canonical_lease_acquire_failed is contention, also transient.
            echo "ambiguous true" ;;
        canonical_lease:*)
            echo "busy true" ;;
        blocked_input_mode|recovery_failed|pane_dead|probe_failed|input_mode_blocked|recovery_cooldown_deferred)
            # Input-mode blocks: terminal is not busy but pane is non-interactive.
            # Requeue after operator resolves copy/search mode.
            # RES-A4: recovery_cooldown_deferred — pane blocked during cooldown, requeueable.
            echo "ambiguous true" ;;
        # DFL-LOG per-code classification (Contract 160 Section 4.4)
        delivery_failed:tx_*|delivery_failed:post_*)
            echo "ambiguous true" ;;
        delivery_failed:pre_skill_*|delivery_failed:pre_instruction_*|delivery_failed:pre_validation_empty_role)
            echo "invalid false" ;;
        delivery_failed:pre_canonical_lease_busy|delivery_failed:pre_legacy_lock_busy|delivery_failed:pre_duplicate_delivery)
            echo "busy true" ;;
        delivery_failed:pre_*)
            echo "ambiguous true" ;;
        *)
            echo "invalid false" ;;
    esac
}

# Emit a structured NDJSON event when a dispatch is blocked.
# Usage: emit_blocked_dispatch_audit <dispatch_id> <terminal_id> <block_reason> [<event_type>] [<failure_code>]
# event_type defaults to "dispatch_blocked"; use "duplicate_delivery_prevented" for duplicates.
# DFL-LOG-2: failure_code and failure_class fields are included when failure_code is provided.
emit_blocked_dispatch_audit() {
    local dispatch_id="$1"
    local terminal_id="$2"
    local block_reason="$3"
    local event_type="${4:-dispatch_blocked}"
    local failure_code="${5:-}"
    local audit_file="$STATE_DIR/blocked_dispatch_audit.ndjson"

    local classification
    classification=$(_classify_blocked_dispatch "$block_reason")
    local block_category="${classification%% *}"
    local requeueable="${classification##* }"

    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    python3 - "$event_type" "$dispatch_id" "$terminal_id" "$block_reason" \
        "$block_category" "$requeueable" "$ts" "$audit_file" "$failure_code" <<'PY'
import json, sys, os
event_type, dispatch_id, terminal_id, block_reason, block_category, requeueable_str, ts, audit_file, failure_code = sys.argv[1:]
event = {
    "event_type": event_type,
    "dispatch_id": dispatch_id,
    "terminal_id": terminal_id,
    "block_reason": block_reason,
    "block_category": block_category,
    "requeueable": requeueable_str == "true",
    "timestamp": ts,
}
if failure_code:
    event["failure_code"] = failure_code
    try:
        sys.path.insert(0, os.path.join(os.environ.get("VNX_HOME", "."), "scripts", "lib"))
        from failure_classifier import FAILURE_CODE_REGISTRY
        entry = FAILURE_CODE_REGISTRY.get(failure_code)
        if entry:
            event["failure_class"] = entry[0]
    except Exception:
        pass
os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
with open(audit_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, separators=(",", ":")) + "\n")
PY
}

# Emit a structured NDJSON audit entry for a lease cleanup outcome.
# Usage: emit_lease_cleanup_audit <dispatch_id> <terminal_id> <event_type> <lease_released> [<error>]
# event_type should be "lease_released_on_failure" or "lease_release_failed"
emit_lease_cleanup_audit() {
    local dispatch_id="$1"
    local terminal_id="$2"
    local event_type="$3"
    local lease_released="$4"
    local error_detail="${5:-}"
    local audit_file="$STATE_DIR/lease_cleanup_audit.ndjson"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    python3 - "$event_type" "$dispatch_id" "$terminal_id" "$lease_released" \
        "$error_detail" "$ts" "$audit_file" <<'PY'
import json, sys, os
event_type, dispatch_id, terminal_id, lease_released, error_detail, ts, audit_file = sys.argv[1:]
event = {
    "event_type": event_type,
    "dispatch_id": dispatch_id,
    "terminal_id": terminal_id,
    "lease_released": lease_released == "true",
    "timestamp": ts,
}
if error_detail:
    event["error"] = error_detail
os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
with open(audit_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, separators=(",", ":")) + "\n")
PY
}
