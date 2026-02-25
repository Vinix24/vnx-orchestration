#!/usr/bin/env bash
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_DIR/lib/_vnx_hook_common.sh"

# Auto-enable rotation when registered as a hook (env var may not propagate
# through all settings.json layers — if this hook is called, it was configured)
export VNX_CONTEXT_ROTATION_ENABLED="${VNX_CONTEXT_ROTATION_ENABLED:-1}"

INPUT="$(cat)"

# Debug: log every invocation to confirm hook is firing
echo "[VNX:ctx_mon $(date +%H:%M:%S)] called, rotation_enabled=${VNX_CONTEXT_ROTATION_ENABLED}, pwd=$PWD" \
  >> "${VNX_LOGS_DIR:-/tmp}/hook_events.log" 2>/dev/null || true

# Loop prevention: if we already blocked and Claude is writing the handover,
# don't block the Write tool call itself.
if command -v jq >/dev/null 2>&1; then
  TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"
  # Allow Write (handover doc), Read, Glob, Grep through — only block action tools
  case "$TOOL_NAME" in
    Write|Read|Glob|Grep) exit 0 ;;
  esac
fi

TERMINAL="$(vnx_detect_terminal)"
case "$TERMINAL" in
  T1|T2|T3) ;;
  *) exit 0 ;;
esac

STATE_FILE="$VNX_STATE_DIR/context_window_${TERMINAL}.json"
[[ -f "$STATE_FILE" ]] || exit 0

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

REMAINING_RAW="$(jq -r '.remaining_pct // empty' "$STATE_FILE" 2>/dev/null || true)"
[[ -n "$REMAINING_RAW" ]] || exit 0

REMAINING_INT="${REMAINING_RAW%.*}"
[[ "$REMAINING_INT" =~ ^-?[0-9]+$ ]] || exit 0

if (( REMAINING_INT < 0 )); then REMAINING_INT=0; fi
if (( REMAINING_INT > 100 )); then REMAINING_INT=100; fi
USED_PCT=$((100 - REMAINING_INT))

WARNING_THRESHOLD=50
ROTATION_THRESHOLD=65  # Must fire before Claude's auto-compact (~80%)

if (( USED_PCT < WARNING_THRESHOLD )); then
  exit 0
fi

# Emit context_pressure data point to ndjson for performance research
# Joins dispatch_id + context% + timestamp for context-rot analysis
_emit_context_pressure() {
  local used_pct="$1" remaining_pct="$2" phase="$3"
  local dispatch_id="unknown"
  local ts_file="$VNX_STATE_DIR/terminal_state.json"
  if [[ -f "$ts_file" ]] && command -v jq >/dev/null 2>&1; then
    dispatch_id=$(jq -r --arg t "$TERMINAL" \
      '.terminals[$t].claimed_by // "unknown"' "$ts_file" 2>/dev/null || echo "unknown")
    dispatch_id="${dispatch_id:-unknown}"
  fi
  local event
  event=$(printf '{"event_type":"context_pressure","terminal":"%s","dispatch_id":"%s","context_used_pct":%d,"context_remaining_pct":%d,"phase":"%s","timestamp":"%s"}' \
    "$TERMINAL" "$dispatch_id" "$used_pct" "$remaining_pct" "$phase" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
  python3 "$VNX_DATA_DIR/../.claude/vnx-system/scripts/append_receipt.py" \
    --receipt "$event" 2>/dev/null || true
}
_PHASE="warning"
(( USED_PCT >= ROTATION_THRESHOLD )) && _PHASE="rotation"
_emit_context_pressure "$USED_PCT" "$REMAINING_INT" "$_PHASE"

if (( USED_PCT >= ROTATION_THRESHOLD )); then
  # Two-stage rotation:
  # Stage 1: block + instruct handover (Claude stays alive to write it)
  # Stage 2: if handover already exists, force-stop Claude so /clear can land
  #
  # Latch file prevents Stage 1 from regenerating a new filename on every
  # hook invocation — Claude sees a consistent filename across retries.

  HANDOVER_DIR="$VNX_DATA_DIR/rotation_handovers"
  LATCH_FILE="$VNX_STATE_DIR/rotation_latch_${TERMINAL}"
  EXISTING_HANDOVER=$(ls -t "$HANDOVER_DIR"/*"${TERMINAL}-ROTATION-HANDOVER"*.md 2>/dev/null | head -1)

  if [[ -n "$EXISTING_HANDOVER" ]]; then
    # Check handover is fresh (written in last 5 minutes = this rotation cycle)
    HANDOVER_MTIME=$(stat -f %m "$EXISTING_HANDOVER" 2>/dev/null || stat -c %Y "$EXISTING_HANDOVER" 2>/dev/null || echo 0)
    HANDOVER_AGE=$(( $(date +%s) - HANDOVER_MTIME ))
    if (( HANDOVER_AGE < 300 )); then
      # Stage 2: handover written — block every subsequent tool call (defense-in-depth).
      # NOTE: {"continue":false} has no effect from PreToolUse; use {"decision":"block"} here.
      # The primary stop is emitted by vnx_handover_detector.sh (PostToolUse) after the Write.
      vnx_log "Context rotation stage 2: handover exists, blocking tool on $TERMINAL"
      cat <<EOF
{"decision":"block","reason":"Context rotation in progress. Handover written to ${EXISTING_HANDOVER}. DO NOT execute any tools — waiting for /clear."}
EOF
      exit 0
    fi
  fi

  # Stage 1: instruct Claude to write handover.
  # Use a latch file so repeated invocations return the SAME filename.
  # Without this, each hook call generates a new timestamp → Claude gets
  # confused by changing filenames and may never write the handover.
  if [[ -f "$LATCH_FILE" ]]; then
    LATCH_AGE=$(( $(date +%s) - $(stat -f %m "$LATCH_FILE" 2>/dev/null || stat -c %Y "$LATCH_FILE" 2>/dev/null || echo 0) ))
    if (( LATCH_AGE < 600 )); then
      # Re-read the latched filename and repeat the same block message
      HANDOVER_FILENAME=$(cat "$LATCH_FILE")
      vnx_log "Context rotation stage 1 (repeat): blocking with latched filename $HANDOVER_FILENAME on $TERMINAL"
      HANDOVER_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
      cat <<EOF
{"decision":"block","reason":"VNX CONTEXT ROTATION REQUIRED (${USED_PCT}% used, ${REMAINING_INT}% remaining). Write a handover file NOW to $VNX_DATA_DIR/rotation_handovers/ named ${HANDOVER_FILENAME}.\n\nREQUIRED FORMAT:\n# ${TERMINAL} Context Rotation Handover\n**Timestamp**: ${HANDOVER_ISO}\n**Terminal**: ${TERMINAL}\n**Dispatch-ID**: [copy from your current dispatch assignment]\n**Context Used**: ${USED_PCT}%\n\n## Status\n[complete | in-progress | blocked]\n\n## Completed Work\n[bullet list of what was done]\n\n## Remaining Tasks\n[bullet list of what is left, or 'None']\n\n## Files Modified\n[list of files changed with brief description]\n\n## Next Steps\n[what the incoming session should do first]\n\nIMPORTANT: Writing the handover file is your FINAL action in this session. Do NOT run any other tools or commands after writing it. The system will handle clearing and resumption automatically."}
EOF
      exit 0
    else
      # Stale latch (>10 min) — remove and re-latch below
      rm -f "$LATCH_FILE"
    fi
  fi

  # First Stage 1 invocation: generate filename and latch it
  vnx_log "Context rotation stage 1: ${USED_PCT}% used on $TERMINAL, requesting handover"
  HANDOVER_TS=$(date +%Y%m%d-%H%M%S)
  HANDOVER_FILENAME="${HANDOVER_TS}-${TERMINAL}-ROTATION-HANDOVER.md"
  echo "$HANDOVER_FILENAME" > "$LATCH_FILE"
  HANDOVER_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  cat <<EOF
{"decision":"block","reason":"VNX CONTEXT ROTATION REQUIRED (${USED_PCT}% used, ${REMAINING_INT}% remaining). Write a handover file NOW to $VNX_DATA_DIR/rotation_handovers/ named ${HANDOVER_FILENAME}.\n\nREQUIRED FORMAT:\n# ${TERMINAL} Context Rotation Handover\n**Timestamp**: ${HANDOVER_ISO}\n**Terminal**: ${TERMINAL}\n**Dispatch-ID**: [copy from your current dispatch assignment]\n**Context Used**: ${USED_PCT}%\n\n## Status\n[complete | in-progress | blocked]\n\n## Completed Work\n[bullet list of what was done]\n\n## Remaining Tasks\n[bullet list of what is left, or 'None']\n\n## Files Modified\n[list of files changed with brief description]\n\n## Next Steps\n[what the incoming session should do first]\n\nIMPORTANT: Writing the handover file is your FINAL action in this session. Do NOT run any other tools or commands after writing it. The system will handle clearing and resumption automatically."}
EOF
  exit 0
fi

# 60-80% used — warning only (log, no block)
vnx_log "Context pressure warning: ${USED_PCT}% used on $TERMINAL"
exit 0
