#!/bin/bash
# Intelligence Refresh Script
# Purpose: Update hash cache and set ACK flag when T0 reads intelligence
# Usage: Call after reading t0_brief.json and progress_state.yaml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
STATE_DIR="$VNX_STATE_DIR"
CACHE_DIR="${STATE_DIR}/cache"
BRIEF="${STATE_DIR}/t0_brief.json"
PROGRESS="${STATE_DIR}/progress_state.yaml"
ACK_FLAG="${CACHE_DIR}/intelligence_ack.flag"
HASH_CACHE="${CACHE_DIR}/intelligence.hash"

# Ensure cache directory exists
mkdir -p "$CACHE_DIR"

# Verify intelligence files exist
if [[ ! -f "$BRIEF" ]]; then
  echo "❌ Error: t0_brief.json not found"
  exit 1
fi

if [[ ! -f "$PROGRESS" ]]; then
  echo "❌ Error: progress_state.yaml not found"
  exit 1
fi

# Calculate current hash
current_hash=$(cat "$BRIEF" "$PROGRESS" | sha256sum | cut -d' ' -f1)

# Update hash cache
echo "$current_hash" > "$HASH_CACHE"

# Set ACK flag
touch "$ACK_FLAG"

# Extract key metrics for output (minimal tokens)
PENDING=$(jq -r '.queues.pending // 0' "$BRIEF" || echo 0)
T1=$(jq -r '.terminals.T1.status // "unknown"' "$BRIEF" || echo "unknown")
T2=$(jq -r '.terminals.T2.status // "unknown"' "$BRIEF" || echo "unknown")
T3=$(jq -r '.terminals.T3.status // "unknown"' "$BRIEF" || echo "unknown")

echo "✅ Intelligence ACK set | T1=$T1 T2=$T2 T3=$T3 Queue=$PENDING"
