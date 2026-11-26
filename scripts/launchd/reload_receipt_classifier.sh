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

source "$SCRIPT_DIR/../lib/vnx_paths.sh"
source "$SCRIPT_DIR/../lib/vnx_resolve_root.sh"
vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
VNX_ROOT="$VNX_HOME_DEFAULT"  # Engine CLI; preserve the consumer project context.
# Config snapshot.
if ! CLASSIFIER_ENABLED="$("$VNX_ROOT/bin/vnx" config get --raw VNX_RECEIPT_CLASSIFIER_ENABLED)"; then
    echo "WARNING: VNX_RECEIPT_CLASSIFIER_ENABLED snapshot unavailable; skipping classifier reload" >&2
    exit 1
fi
# End config snapshot.

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

# Capture first: `launchctl list | grep -q` ends on 141 under pipefail (SIGPIPE) after a match.
LOADED_AGENTS="$(launchctl list)"
if grep -q "$PLIST_LABEL" <<<"$LOADED_AGENTS"; then
    echo "OK: agent registered in launchctl"
else
    echo "WARNING: agent not found in launchctl list — check $PLIST_DEST" >&2
    exit 1
fi

echo "Logs: /tmp/vnx-receipt-classifier-batch.log / /tmp/vnx-receipt-classifier-batch.err"
echo
echo "VNX_RECEIPT_CLASSIFIER_ENABLED=$CLASSIFIER_ENABLED (effective configuration)"
echo "Enable with: $VNX_ROOT/bin/vnx config set VNX_RECEIPT_CLASSIFIER_ENABLED 1"
