#!/usr/bin/env bash
set -euo pipefail

TERMINAL="${1:?Usage: vnx_rotate.sh TERMINAL /path/to/handover.md}"
HANDOVER_PATH="${2:?Usage: vnx_rotate.sh TERMINAL /path/to/handover.md}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

_early_cleanup() {
  rm -rf "$_PROJECT_ROOT/.vnx-data/locks/rotation_${TERMINAL}.lock" 2>/dev/null || true
}
trap _early_cleanup EXIT INT TERM

if [[ "${VNX_CONTEXT_ROTATION_ENABLED:-0}" != "1" ]]; then
  exit 0
fi

source "$SCRIPT_DIR/lib/_vnx_hook_common.sh"
source "$SCRIPT_DIR/../scripts/pane_config.sh"
source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"

mkdir -p "$VNX_LOGS_DIR" "$VNX_STATE_DIR" 2>/dev/null || true
LOG="$VNX_LOGS_DIR/vnx_rotate_${TERMINAL}.log"
SIGNAL_FILE="$VNX_STATE_DIR/rotation_clear_done_${TERMINAL}"

log() {
  echo "[$(date +%H:%M:%S)] $*" >> "$LOG" 2>/dev/null || true
}

cleanup() {
  vnx_release_lock "rotation_${TERMINAL}"
  log "Lock released (cleanup)"
}
trap cleanup EXIT INT TERM

PANE_ID="$(get_pane_id "$TERMINAL")"
log "Pane resolved: $TERMINAL -> $PANE_ID"

if ! tmux has-session -t vnx 2>/dev/null; then
  log "ERROR: No vnx tmux session found"
  exit 1
fi

log "Starting context rotation for $TERMINAL"
rm -f "$SIGNAL_FILE"

# Remove the rotation latch file so the context monitor stops blocking
# after /clear lands and the new session starts with a fresh context window.
LATCH_FILE="$VNX_STATE_DIR/rotation_latch_${TERMINAL}"
rm -f "$LATCH_FILE"
log "Rotation latch removed: $LATCH_FILE"

sleep 3

log "Sending /clear to pane $PANE_ID"
# Pre-clear input line (C-u only — C-c would kill the CLI process)
tmux send-keys -t "$PANE_ID" C-u 2>/dev/null || true
sleep 0.3
tmux send-keys -t "$PANE_ID" -l "/clear"
sleep 1  # Allow CLI to fully render typed command before submitting
tmux send-keys -t "$PANE_ID" Enter
sleep 3  # Post-clear delay (Claude needs ~3s to reset UI)

log "Waiting for /clear completion..."
WAITED=0
while [[ ! -f "$SIGNAL_FILE" ]] && (( WAITED < 15 )); do
  sleep 1
  WAITED=$((WAITED + 1))
done

if [[ ! -f "$SIGNAL_FILE" ]]; then
  log "WARNING: Signal file not created after 15s, proceeding with fallback delay"
  sleep 5
fi
rm -f "$SIGNAL_FILE"

# --- Skill recovery: extract from original dispatch ---
DISPATCH_ID=""
DISPATCH_FILE=""
SKILL=""

# Strategy B (macOS-safe sed): extract Dispatch-ID from handover
# [^:]* absorbs optional markdown bold (**) or other non-colon chars before the colon
DISPATCH_ID=$(sed -n 's/.*Dispatch-ID[^:]*:[[:space:]]*\([A-Za-z0-9_-]*\).*/\1/p' "$HANDOVER_PATH" | head -1)
if [[ -z "$DISPATCH_ID" ]]; then
  DISPATCH_ID=$(sed -n 's/.*Dispatch[^:]*:[[:space:]]*\([A-Za-z0-9_-]*\).*/\1/p' "$HANDOVER_PATH" | head -1)
fi

if [[ -n "$DISPATCH_ID" ]]; then
  DISPATCH_FILE=$(ls "$VNX_DATA_DIR/dispatches/active/${DISPATCH_ID}"*.md \
                     "$VNX_DATA_DIR/dispatches/completed/${DISPATCH_ID}"*.md 2>/dev/null | head -1)
fi

if [[ -n "$DISPATCH_FILE" ]] && [[ -f "$DISPATCH_FILE" ]]; then
  ROLE=$(vnx_dispatch_extract_agent_role "$DISPATCH_FILE")
  if [[ -n "$ROLE" ]]; then
    # Validate role is a known skill (already validated at dispatch time)
    if python3 "$SCRIPT_DIR/../scripts/validate_skill.py" "$ROLE" >/dev/null 2>&1; then
      SKILL="$ROLE"
      log "Skill recovered from dispatch: /$SKILL (dispatch=$DISPATCH_ID)"
    else
      log "WARNING: Role '$ROLE' not a valid skill, skipping skill activation"
    fi
  fi
else
  log "WARNING: Could not find dispatch file for ID='$DISPATCH_ID', falling back to plain prompt"
fi

# --- Send continuation prompt (hybrid dispatch pattern) ---
if [[ -n "$SKILL" ]]; then
  # Skill activation via send-keys (matches dispatcher pattern)
  tmux send-keys -t "$PANE_ID" C-u 2>/dev/null || true
  sleep 0.3
  tmux send-keys -t "$PANE_ID" -l "/${SKILL}"
  sleep 0.5

  PROMPT=" Continue dispatch ${DISPATCH_ID}.

Read handover: ${HANDOVER_PATH}
Read dispatch: ${DISPATCH_FILE}

Resume from where the previous session left off. The handover doc contains status, completed work, and remaining tasks."
else
  PROMPT="Context rotation complete. Read the handover document at:
${HANDOVER_PATH}

Resume the remaining work as described in the handover document. Start by reading that file."
fi

log "Sending continuation prompt (skill=${SKILL:-none})"
echo "$PROMPT" | tmux load-buffer -
tmux paste-buffer -t "$PANE_ID"
sleep 1
tmux send-keys -t "$PANE_ID" Enter

# Update terminal state to "working" so T0 sees correct status after rotation
if [[ -n "$DISPATCH_ID" && "$DISPATCH_ID" != "unknown" ]]; then
  python3 "$SCRIPT_DIR/../scripts/terminal_state_shadow.py" \
    --terminal-id "$TERMINAL" \
    --status "working" \
    --claimed-by "$DISPATCH_ID" \
    --last-activity "$(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null || \
    log "WARNING: Failed to update terminal state after rotation"
  log "Terminal state updated: $TERMINAL -> working (dispatch=$DISPATCH_ID)"
fi

# Emit continuation receipt for context-rot research
# Links dispatch_id + handover + session number for multi-continuation analysis
CONTEXT_USED_PCT=0
CTX_FILE="$VNX_STATE_DIR/context_window_${TERMINAL}.json"
if [[ -f "$CTX_FILE" ]]; then
  REMAINING_AT_ROTATION=$(python3 -c "import json; d=json.load(open('$CTX_FILE')); print(d.get('remaining_pct',0))" 2>/dev/null || echo 0)
  CONTEXT_USED_PCT=$(( 100 - REMAINING_AT_ROTATION ))
fi

CONTINUATION_RECEIPT=$(printf \
  '{"event_type":"context_rotation_continuation","terminal":"%s","dispatch_id":"%s","handover_path":"%s","skill":"%s","context_used_pct_at_rotation":%d,"timestamp":"%s"}' \
  "$TERMINAL" "${DISPATCH_ID:-unknown}" "$HANDOVER_PATH" "${SKILL:-none}" \
  "$CONTEXT_USED_PCT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")

python3 "$SCRIPT_DIR/../scripts/append_receipt.py" \
  --receipt "$CONTINUATION_RECEIPT" 2>/dev/null || \
  log "WARNING: Failed to emit continuation receipt"

log "Rotation complete for $TERMINAL (dispatch=${DISPATCH_ID:-unknown}, context_at_rotation=${CONTEXT_USED_PCT}%)"
exit 0
