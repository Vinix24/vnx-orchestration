#!/usr/bin/env bash
# Reload the com.vnx.conversation-analyzer launchd agent.
#
# Usage: bash scripts/launchd/reload_conversation_analyzer.sh
#
# Requires $VNX_HOME to be set (or export VNX_HOME=<path-to-repo> before running).
# Substitutes ${VNX_HOME} in the plist template and loads the agent.
#
# After merge, run this once to re-enable the nightly analyzer on your system.

set -euo pipefail

PLIST_LABEL="com.vnx.conversation-analyzer"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/${PLIST_LABEL}.plist"

if [ -z "${VNX_HOME:-}" ]; then
    # Derive from the location of this script: scripts/launchd/ → repo root
    VNX_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
    echo "VNX_HOME not set — using derived path: $VNX_HOME"
fi

if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: plist template not found: $PLIST_SRC" >&2
    exit 1
fi

TARGET_SCRIPT="$VNX_HOME/scripts/conversation_analyzer_nightly.sh"
if [ ! -f "$TARGET_SCRIPT" ]; then
    echo "ERROR: target script not found: $TARGET_SCRIPT" >&2
    echo "  VNX_HOME=$VNX_HOME may be wrong. Set VNX_HOME to the repo root and retry." >&2
    exit 1
fi

# Unload existing agent if present (ignore errors — may not be loaded)
if [ -f "$PLIST_DEST" ]; then
    echo "Unloading existing agent: $PLIST_LABEL"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

# Substitute ${VNX_HOME} placeholder with actual path and write to LaunchAgents
echo "Writing resolved plist to: $PLIST_DEST"
sed "s|\${VNX_HOME}|$VNX_HOME|g" "$PLIST_SRC" > "$PLIST_DEST"

# Load the agent
launchctl load "$PLIST_DEST"
echo "Loaded: $PLIST_LABEL (runs nightly at 02:00)"

# Verify it appears in launchctl list
if launchctl list | grep -q "$PLIST_LABEL"; then
    echo "OK: agent registered in launchctl"
else
    echo "WARNING: agent not found in launchctl list — check $PLIST_DEST" >&2
    exit 1
fi

echo "Logs: /tmp/vnx-conversation-analyzer.log / /tmp/vnx-conversation-analyzer.err"
