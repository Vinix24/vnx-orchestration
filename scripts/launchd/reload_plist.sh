#!/usr/bin/env bash
# Generic launchd plist reload helper.
#
# Usage: bash scripts/launchd/reload_plist.sh <name>
#   <name> — plist filename without .plist extension
#            e.g. com.vnx.conversation-analyzer
#
# Requires $VNX_HOME to be set, or derives from script location (scripts/launchd/ → repo root).
# Substitutes ${VNX_HOME} in the plist template, writes resolved plist to
# ~/Library/LaunchAgents/<name>.plist, and reloads via launchctl.
#
# Returns 0 on success, non-zero on failure.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <name>" >&2
    echo "  <name> — plist name without .plist (e.g. com.vnx.conversation-analyzer)" >&2
    exit 1
fi

PLIST_LABEL="$1"
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

# Unload existing agent if present (ignore errors — may not be loaded yet)
if [ -f "$PLIST_DEST" ]; then
    echo "Unloading existing agent: $PLIST_LABEL"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

# Substitute ${VNX_HOME} placeholder and write to LaunchAgents
echo "Writing resolved plist to: $PLIST_DEST"
sed "s|\${VNX_HOME}|$VNX_HOME|g" "$PLIST_SRC" > "$PLIST_DEST"

# Load the agent
launchctl load "$PLIST_DEST"
echo "Loaded: $PLIST_LABEL"

# Verify the agent appears in launchctl list
if launchctl list | grep -q "$PLIST_LABEL"; then
    echo "OK: agent registered in launchctl"
else
    echo "WARNING: agent not found in launchctl list — check $PLIST_DEST" >&2
    exit 1
fi
