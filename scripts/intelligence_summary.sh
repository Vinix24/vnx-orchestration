#!/bin/bash
# Minimal intelligence summary injector
# Token budget: 100-150 tokens
# Version: 2.0
# Purpose: Provide condensed system state at each turn

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
STATE_DIR="$VNX_STATE_DIR"
BRIEF="${STATE_DIR}/t0_brief.json"
PROGRESS="${STATE_DIR}/progress_state.yaml"

# Check if intelligence files exist
if [[ ! -f "$BRIEF" ]]; then
  echo "⚠️ t0_brief.json missing - refresh intelligence"
  exit 0
fi

if [[ ! -f "$PROGRESS" ]]; then
  echo "⚠️ progress_state.yaml missing - refresh intelligence"
  exit 0
fi

# Extract key fields using jq
PENDING=$(jq -r '.queues.pending // 0' "$BRIEF" || echo 0)
T1_STATUS=$(jq -r '.terminals.T1.status // "unknown"' "$BRIEF" || echo "unknown")
T2_STATUS=$(jq -r '.terminals.T2.status // "unknown"' "$BRIEF" || echo "unknown")
T3_STATUS=$(jq -r '.terminals.T3.status // "unknown"' "$BRIEF" || echo "unknown")

TRACK_A_GATE=$(jq -r '.tracks.A.current_gate // "unknown"' "$BRIEF" || echo "unknown")
TRACK_B_GATE=$(jq -r '.tracks.B.current_gate // "unknown"' "$BRIEF" || echo "unknown")
TRACK_C_GATE=$(jq -r '.tracks.C.current_gate // "unknown"' "$BRIEF" || echo "unknown")

# Minimal summary (50-100 tokens)
cat <<EOF
📊 Intelligence: T1=$T1_STATUS@$TRACK_A_GATE | T2=$T2_STATUS@$TRACK_B_GATE | T3=$T3_STATUS@$TRACK_C_GATE | Queue=$PENDING
EOF
