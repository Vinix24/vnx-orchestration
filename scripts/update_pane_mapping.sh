#!/bin/bash
# Update pane mapping based on current tmux session
# This script should be run whenever tmux panes change

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
# shellcheck source=lib/ops_process_control.sh
source "$SCRIPT_DIR/lib/ops_process_control.sh"
PANES_FILE="$VNX_STATE_DIR/panes.json"
PROCESS_LOG="$VNX_LOGS_DIR/process_lifecycle.log"
mkdir -p "$VNX_LOGS_DIR"

# Determine which session to use
PROJECT_SESSION="vnx-$(basename "$VNX_DIR")"
if tmux has-session -t vnx 2>/dev/null; then
    SESSION="vnx"
elif tmux has-session -t "$PROJECT_SESSION" 2>/dev/null; then
    SESSION="$PROJECT_SESSION"
else
    echo "ERROR: No vnx or $PROJECT_SESSION session found"
    exit 1
fi

echo "Updating pane mapping for session: $SESSION"

# Preserve existing T1 provider across remaps
CURRENT_T1_PROVIDER="${VNX_T1_PROVIDER:-}"
if [ -z "$CURRENT_T1_PROVIDER" ] && command -v jq >/dev/null 2>&1 && [ -f "$PANES_FILE" ]; then
    CURRENT_T1_PROVIDER="$(jq -r '.T1.provider // empty' "$PANES_FILE" 2>/dev/null || true)"
fi
CURRENT_T1_PROVIDER="${CURRENT_T1_PROVIDER:-claude_code}"

# Get current pane IDs from tmux
T0_PANE=$(tmux list-panes -t "$SESSION:0" -F "#{pane_id}" | sed -n '1p')
T2_PANE=$(tmux list-panes -t "$SESSION:0" -F "#{pane_id}" | sed -n '2p')
T1_PANE=$(tmux list-panes -t "$SESSION:0" -F "#{pane_id}" | sed -n '3p')
T3_PANE=$(tmux list-panes -t "$SESSION:0" -F "#{pane_id}" | sed -n '4p')

# Create updated panes.json
cat > "$PANES_FILE" <<EOF
{
  "session": "$SESSION",
  "t0": {
    "pane_id": "$T0_PANE",
    "role": "orchestrator",
    "do_not_target": true,
    "model": "opus",
    "provider": "claude_code"
  },
  "T0": {
    "pane_id": "$T0_PANE",
    "role": "orchestrator",
    "do_not_target": true,
    "model": "opus",
    "provider": "claude_code"
  },
  "T1": {
    "pane_id": "$T1_PANE",
    "track": "A",
    "model": "sonnet",
    "provider": "$CURRENT_T1_PROVIDER"
  },
  "T2": {
    "pane_id": "$T2_PANE",
    "track": "B",
    "model": "sonnet",
    "provider": "claude_code"
  },
  "T3": {
    "pane_id": "$T3_PANE",
    "track": "C",
    "model": "opus",
    "role": "deep",
    "provider": "claude_code"
  },
  "tracks": {
    "A": {
      "pane_id": "$T1_PANE",
      "track": "A",
      "model": "sonnet",
      "provider": "$CURRENT_T1_PROVIDER"
    },
    "B": {
      "pane_id": "$T2_PANE",
      "track": "B",
      "model": "sonnet",
      "provider": "claude_code"
    },
    "C": {
      "pane_id": "$T3_PANE",
      "track": "C",
      "model": "opus",
      "role": "deep",
      "provider": "claude_code"
    }
  }
}
EOF

echo "Pane mapping updated:"
echo "  T0: $T0_PANE (Orchestrator)"
echo "  T1: $T1_PANE (Track A)"
echo "  T2: $T2_PANE (Track B)"
echo "  T3: $T3_PANE (Track C)"
echo ""
echo "Saved to: $PANES_FILE"

# Restart smart_tap to pick up new mapping
echo "Restarting smart_tap with new pane mapping..."
vnx_stop_by_fingerprints "smart_tap" "$PROCESS_LOG" "pane_mapping_refresh" 3 \
  "smart_tap_v7_json_translator.sh" \
  "smart_tap_with_editor_multi.sh" \
  "smart_tap_with_editor.sh" \
  "smart_tap_clean.sh" \
  "smart_tap_hybrid.sh" || true
sleep 1
cd "$VNX_DIR/scripts"
nohup ./smart_tap_v7_json_translator.sh > /dev/null 2>&1 &
echo "Smart tap restarted"
