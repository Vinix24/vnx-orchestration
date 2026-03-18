#!/bin/bash
# Queue Auto-Accept Watcher
# Replaces queue_popup_watcher when VNX_QUEUE_POPUP_ENABLED=0
# Moves dispatches from queue/ → pending/ without user interaction

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

source "$VNX_HOME/scripts/singleton_enforcer.sh"
enforce_singleton "queue_auto_accept"

QUEUE_DIR="$VNX_DISPATCH_DIR/queue"
PENDING_DIR="$VNX_DISPATCH_DIR/pending"
STATE_DIR="$VNX_STATE_DIR"

mkdir -p "$QUEUE_DIR" "$PENDING_DIR"

echo "Queue Auto-Accept Watcher starting..."
echo "Mode: auto-accept (no popup, no user approval)"
echo "Monitoring: $QUEUE_DIR → $PENDING_DIR"

while true; do
    moved=0
    for f in "$QUEUE_DIR"/*.md; do
        [ -f "$f" ] || continue
        filename="$(basename "$f")"
        target="$PENDING_DIR/$filename"

        if [ -f "$target" ]; then
            echo "[auto-accept] Already in pending, removing queue duplicate: $filename"
            rm -f "$f"
            continue
        fi

        mv "$f" "$target"
        moved=$((moved + 1))
        echo "[auto-accept] $(date +%H:%M:%S) Moved to pending: $filename"
    done

    if [ "$moved" -gt 0 ]; then
        echo "[auto-accept] Auto-accepted $moved dispatch(es)"
    fi

    sleep 2
done
