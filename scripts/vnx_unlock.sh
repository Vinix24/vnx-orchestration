#!/usr/bin/env bash
# vnx_unlock.sh — Unlock blocked terminals by resetting status to idle and clearing claims.
# Usage: bash vnx_unlock.sh T1 [T2 T3]
# Usage: bash vnx_unlock.sh --all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATE_DIR="$REPO_ROOT/.vnx-data/state"
STATE_FILE="$STATE_DIR/terminal_state.json"
SHADOW_SCRIPT="$SCRIPT_DIR/terminal_state_shadow.py"
PROGRESS_SCRIPT="$SCRIPT_DIR/update_progress_state.py"

if [[ $# -eq 0 ]]; then
    echo "Usage: bash vnx_unlock.sh T1 [T2 T3 ...]"
    echo "       bash vnx_unlock.sh --all"
    exit 1
fi

# Resolve terminal list
TERMINALS=()
if [[ "$1" == "--all" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "No terminal_state.json found at $STATE_FILE"
        exit 1
    fi
    while IFS= read -r tid; do
        TERMINALS+=("$tid")
    done < <(python3 -c "import json; d=json.load(open('$STATE_FILE')); print('\n'.join(d.get('terminals',{}).keys()))")
else
    TERMINALS=("$@")
fi

if [[ ${#TERMINALS[@]} -eq 0 ]]; then
    echo "No terminals to unlock."
    exit 0
fi

# Unlock each terminal
for TID in "${TERMINALS[@]}"; do
    echo "Unlocking $TID ..."
    python3 "$SHADOW_SCRIPT" \
        --terminal-id "$TID" \
        --status idle \
        --clear-claim > /dev/null
    echo "  $TID → idle (claim cleared)"
done

# Sync progress state
if [[ -f "$PROGRESS_SCRIPT" ]]; then
    python3 "$PROGRESS_SCRIPT" --sync 2>/dev/null || true
fi

echo "Done. Unlocked: ${TERMINALS[*]}"
