#!/usr/bin/env bash
# Install VNX nightly intelligence pipeline as a launchd agent.
# Runs the pipeline daily at 02:00.
#
# Usage: bash scripts/launchd/install-nightly-pipeline.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/nightly_intelligence_pipeline.sh"
PLIST_SRC="$SCRIPT_DIR/com.vnx.nightly-intelligence-pipeline.plist"
PLIST_LABEL="com.vnx.nightly-intelligence-pipeline"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
OLD_PLIST_LABEL="com.vnx.conversation-analyzer"
OLD_PLIST_DEST="$HOME/Library/LaunchAgents/${OLD_PLIST_LABEL}.plist"

if [ ! -f "$PIPELINE_SCRIPT" ]; then
    echo "ERROR: pipeline script not found at $PIPELINE_SCRIPT" >&2
    exit 1
fi

# Unload old conversation-analyzer plist if present
if [ -f "$OLD_PLIST_DEST" ]; then
    echo "Unloading old plist: $OLD_PLIST_DEST"
    launchctl unload "$OLD_PLIST_DEST" 2>/dev/null || true
fi

# Copy plist, substituting the actual script path
echo "Installing plist to $PLIST_DEST"
sed "s|__VNX_PIPELINE_SCRIPT__|$PIPELINE_SCRIPT|g" "$PLIST_SRC" > "$PLIST_DEST"

# Unload any existing version before reloading
launchctl unload "$PLIST_DEST" 2>/dev/null || true

# Load the new plist
launchctl load "$PLIST_DEST"
echo "Loaded: $PLIST_LABEL (runs daily at 02:00)"
echo "Logs: /tmp/vnx-nightly-pipeline.log / /tmp/vnx-nightly-pipeline.err"
