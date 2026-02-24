#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/_vnx_hook_common.sh"

vnx_context_rotation_enabled || exit 0

INPUT="$(cat)"
TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"
FILE_PATH="$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")"

if [[ "$TOOL_NAME" != "Write" ]] || [[ "$FILE_PATH" != *"ROTATION-HANDOVER"* ]]; then
  exit 0
fi

TERMINAL="$(vnx_detect_terminal)"
if [[ "$TERMINAL" == "unknown" ]] || [[ "$TERMINAL" == "T0" ]] || [[ "$TERMINAL" == "T-MANAGER" ]]; then
  exit 0
fi

vnx_log "Handover doc detected: $FILE_PATH (terminal: $TERMINAL)"

LOCK_NAME="rotation_${TERMINAL}"
if ! vnx_acquire_lock "$LOCK_NAME"; then
  vnx_log "Rotation already in progress for $TERMINAL, skipping"
  exit 0
fi
vnx_log "Lock acquired: $LOCK_NAME"

_detector_cleanup() {
  if [[ -d "$VNX_LOCKS_DIR/${LOCK_NAME}.lock" ]]; then
    vnx_log "Detector cleanup releasing lock: $LOCK_NAME"
  fi
  vnx_release_lock "$LOCK_NAME"
}
trap _detector_cleanup EXIT INT TERM

REMAINING_INT=0
if [[ -f "$VNX_STATE_DIR/context_window.json" ]]; then
  REMAINING_RAW="$(jq -r '.remaining_pct // 0' "$VNX_STATE_DIR/context_window.json" 2>/dev/null || echo "0")"
  REMAINING_INT="${REMAINING_RAW%.*}"
  [[ "$REMAINING_INT" =~ ^-?[0-9]+$ ]] || REMAINING_INT=0
fi
USED_PCT=$((100 - REMAINING_INT))
if (( USED_PCT < 0 )); then USED_PCT=0; fi
if (( USED_PCT > 100 )); then USED_PCT=100; fi

RECEIPT_JSON=$(cat <<RECEIPT_EOF
{
  "event_type": "context_rotation",
  "event": "context_rotation",
  "terminal": "$TERMINAL",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "source": "vnx_rotation",
  "handover_path": "$FILE_PATH",
  "context_used_pct": $USED_PCT,
  "action_required": false,
  "auto_generated": true
}
RECEIPT_EOF
)

# Testability override: lets unit tests inject a failing/mock append helper without
# changing production scripts. Default behavior remains the canonical append helper.
APPEND_RECEIPT_SCRIPT="${VNX_APPEND_RECEIPT_SCRIPT:-$SCRIPT_DIR/../scripts/append_receipt.py}"
python3 "$APPEND_RECEIPT_SCRIPT" --receipt "$RECEIPT_JSON" 2>/dev/null || \
  vnx_log "WARN: Failed to append rotation receipt"

# Testability override: lets tests simulate missing/immediate-exit rotator behavior.
# Default behavior remains the local vnx_rotate.sh script.
ROTATE_SCRIPT="${VNX_ROTATE_SCRIPT:-$SCRIPT_DIR/vnx_rotate.sh}"
if [[ ! -x "$ROTATE_SCRIPT" ]]; then
  vnx_log "ERROR: vnx_rotate.sh not found or not executable"
  exit 0
fi

mkdir -p "$VNX_LOGS_DIR" 2>/dev/null || true
nohup "$ROTATE_SCRIPT" "$TERMINAL" "$FILE_PATH" \
  > "$VNX_LOGS_DIR/vnx_rotate_${TERMINAL}.log" 2>&1 &
ROTATE_PID=$!

# v2.4 lock handoff rule:
# Keep the detector trap active until the child proves it survived startup.
_child_running_non_zombie() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  if command -v ps >/dev/null 2>&1; then
    local pstate
    pstate="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$pstate" ]] || return 1
    [[ "$pstate" == Z* ]] && return 1
  fi
  return 0
}

if ! _child_running_non_zombie "$ROTATE_PID"; then
  vnx_log "ERROR: vnx_rotate.sh exited immediately (PID: $ROTATE_PID)"
  exit 0
fi

for _startup_check in 1 2 3 4 5; do
  sleep 0.2
  if ! _child_running_non_zombie "$ROTATE_PID"; then
    vnx_log "ERROR: vnx_rotate.sh exited during startup (PID: $ROTATE_PID)"
    exit 0
  fi
done

trap - EXIT INT TERM
vnx_log "Rotation script started for $TERMINAL (PID: $ROTATE_PID)"

exit 0
