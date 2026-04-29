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

        # Emit dispatch_created BEFORE the mv so register-driven queue/status
        # views never observe a file in pending/ without the canonical
        # creation event. dispatch_register.py documents dispatch_created as
        # the canonical "written to pending/" event — emitting after the mv
        # means a transient register-write failure leaves the dispatch
        # invisible to register-backed reporting forever (file is already in
        # pending/ and will not be retried). Best-effort: emit failures are
        # surfaced (with captured stderr) but still allow the mv to proceed
        # so queue items do not back up indefinitely on a transient failure.
        _dispatch_id="$(basename "$f" .md)"
        _reg_rc=0
        _reg_stderr=""
        set +e
        _reg_stderr=$(python3 "$VNX_HOME/scripts/lib/dispatch_register.py" append dispatch_created \
            "dispatch_id=$_dispatch_id" 2>&1 >/dev/null)
        _reg_rc=$?
        set -e
        if [ "$_reg_rc" -ne 0 ]; then
            echo "[auto-accept] WARNING: dispatch_created emit failed for $_dispatch_id rc=$_reg_rc stderr=${_reg_stderr} (non-fatal — proceeding with mv)"
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
