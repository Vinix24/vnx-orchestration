#!/usr/bin/env bash
# Temporary payload logger — deploy, trigger once, inspect, remove.
# Purpose: verify exact JSON structure Claude sends to Stop hooks.
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_DIR/lib/_vnx_hook_common.sh"

INPUT=$(cat)
LOGFILE="$VNX_LOGS_DIR/hook_payload_probe.log"
mkdir -p "$VNX_LOGS_DIR"

{
  echo "=== STOP HOOK PAYLOAD $(date -u +%Y-%m-%dT%H:%M:%S) ==="
  echo "PWD: $PWD"
  echo "STDIN:"
  echo "$INPUT"
  echo "=== END ==="
} >> "$LOGFILE"

# Always approve — this is a probe, not a blocker
echo '{"decision":"approve"}'
