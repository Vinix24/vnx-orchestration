#!/bin/bash
# SessionStart Hook for T0 Intelligence
# Purpose: Inject minimal intelligence summary at session start
# Token budget: 50-100 tokens
# Only outputs if intelligence changed since last session

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
STATE_DIR="$VNX_STATE_DIR"
CACHE_DIR="${STATE_DIR}/cache"
BRIEF="${STATE_DIR}/t0_brief.json"
LAST_SUMMARY="${CACHE_DIR}/last_summary.txt"

# Ensure cache directory exists
mkdir -p "$CACHE_DIR"

# Check if intelligence files exist
if [[ ! -f "$BRIEF" ]]; then
  cat <<EOF
⚠️ Intelligence Missing

t0_brief.json not found. System state unknown.
Run state refresh before creating dispatches.
EOF
  exit 0
fi

# Extract current summary. The `|| echo` guards keep a truncated/partially
# written brief (a real race while state refresh rewrites it) from aborting the
# hook now that errexit is live; the `// default` filters handle missing keys.
PENDING=$(jq -r '.queues.pending // 0' "$BRIEF" || echo 0)
T1=$(jq -r '.terminals.T1.status // "?"' "$BRIEF" || echo "?")
T2=$(jq -r '.terminals.T2.status // "?"' "$BRIEF" || echo "?")
T3=$(jq -r '.terminals.T3.status // "?"' "$BRIEF" || echo "?")
T1_GATE=$(jq -r '.tracks.A.current_gate // "?"' "$BRIEF" || echo "?")
T2_GATE=$(jq -r '.tracks.B.current_gate // "?"' "$BRIEF" || echo "?")
T3_GATE=$(jq -r '.tracks.C.current_gate // "?"' "$BRIEF" || echo "?")

current_summary="T1=${T1}@${T1_GATE}|T2=${T2}@${T2_GATE}|T3=${T3}@${T3_GATE}|Q=${PENDING}"

# Check if summary changed
if [[ -f "$LAST_SUMMARY" ]]; then
  last_summary=$(cat "$LAST_SUMMARY")
  if [[ "$current_summary" == "$last_summary" ]]; then
    # No change - output minimal reminder
    echo "📋 Intelligence: Read t0_brief.json + progress_state.yaml before dispatches"
    exit 0
  fi
fi

# Summary changed - output full context
cat <<EOF
📊 Intelligence Update:
T1=$T1@$T1_GATE | T2=$T2@$T2_GATE | T3=$T3@$T3_GATE | Queue=$PENDING

Before creating dispatches:
1. Read t0_brief.json and progress_state.yaml
2. Run: $VNX_HOME/scripts/intelligence_refresh.sh
3. Create dispatch (hook validates ACK)
EOF

# Cache current summary
echo "$current_summary" > "$LAST_SUMMARY"
