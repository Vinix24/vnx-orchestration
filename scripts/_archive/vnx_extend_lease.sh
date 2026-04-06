#!/usr/bin/env bash
# vnx_extend_lease.sh - Extend lease for a terminal working on a long task
# Usage: bash vnx_extend_lease.sh TERMINAL_ID [--lease-seconds N]
# Example: bash vnx_extend_lease.sh T3
#          bash vnx_extend_lease.sh T3 --lease-seconds 3600

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

TERMINAL_ID="${1:?Usage: $0 TERMINAL_ID [--lease-seconds N]}"
LEASE_SECONDS="${VNX_DISPATCH_LEASE_SECONDS:-600}"
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lease-seconds) LEASE_SECONDS="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

case "$TERMINAL_ID" in
    T1|T2|T3) ;;
    *) echo "Invalid terminal ID: $TERMINAL_ID (must be T1, T2, or T3)" >&2; exit 1 ;;
esac

STATE_FILE="$VNX_STATE_DIR/terminal_state.json"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Read current claimed_by and status to preserve them
read -r CLAIMED_BY CURRENT_STATUS <<< "$(python3 - <<PY 2>/dev/null || echo " working"
import json, sys
from pathlib import Path
try:
    doc = json.loads(Path("$STATE_FILE").read_text())
    r = doc.get("terminals", {}).get("$TERMINAL_ID", {})
    print(r.get("claimed_by") or "", r.get("status") or "working")
except Exception:
    print("", "working")
PY
)"

echo "[extend_lease] $TERMINAL_ID: extending lease +${LEASE_SECONDS}s (claimed_by: ${CLAIMED_BY:-none}, status: ${CURRENT_STATUS:-working})"

python3 "$SCRIPT_DIR/terminal_state_shadow.py" \
    --terminal-id "$TERMINAL_ID" \
    --status "${CURRENT_STATUS:-working}" \
    --claimed-by "${CLAIMED_BY:-}" \
    --lease-seconds "$LEASE_SECONDS" \
    --last-activity "$NOW_ISO" \
    --state-dir "$VNX_STATE_DIR"

echo "[extend_lease] Done. $TERMINAL_ID lease active until +${LEASE_SECONDS}s from now."
