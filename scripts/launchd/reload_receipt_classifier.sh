#!/usr/bin/env bash
# Reload the com.vnx.receipt-classifier-batch launchd agent.
#
# Usage: bash scripts/launchd/reload_receipt_classifier.sh
#
# Substitutes ${VNX_HOME} in the plist template and (re)loads the agent.
# After merge, run this once to schedule the hourly classifier batch job.
# IMPORTANT: the plist ships with VNX_RECEIPT_CLASSIFIER_ENABLED=0 so it is
# inert until the operator flips that flag.

set -euo pipefail

PLIST_LABEL="com.vnx.receipt-classifier-batch"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/${PLIST_LABEL}.plist"

if [ -z "${VNX_HOME:-}" ]; then
    VNX_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
    echo "VNX_HOME not set — using derived path: $VNX_HOME"
fi

if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: plist template not found: $PLIST_SRC" >&2
    exit 1
fi

TARGET_SCRIPT="$VNX_HOME/scripts/lib/receipt_classifier_batch.py"
if [ ! -f "$TARGET_SCRIPT" ]; then
    echo "ERROR: target script not found: $TARGET_SCRIPT" >&2
    echo "  VNX_HOME=$VNX_HOME may be wrong. Set VNX_HOME to the repo root and retry." >&2
    exit 1
fi

if [ -f "$PLIST_DEST" ]; then
    echo "Unloading existing agent: $PLIST_LABEL"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

echo "Writing resolved plist to: $PLIST_DEST"
sed "s|\${VNX_HOME}|$VNX_HOME|g" "$PLIST_SRC" > "$PLIST_DEST"

launchctl load "$PLIST_DEST"
echo "Loaded: $PLIST_LABEL (runs hourly)"

if launchctl list | grep -q "$PLIST_LABEL"; then
    echo "OK: agent registered in launchctl"
else
    echo "WARNING: agent not found in launchctl list — check $PLIST_DEST" >&2
    exit 1
fi

echo "Logs: /tmp/vnx-receipt-classifier-batch.log / /tmp/vnx-receipt-classifier-batch.err"
echo
echo "NOTE: the plist ships with VNX_RECEIPT_CLASSIFIER_ENABLED=0."
echo "      Edit \$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist and re-run this"
echo "      script to enable. Or export the env var in your shell when invoking ad-hoc."
