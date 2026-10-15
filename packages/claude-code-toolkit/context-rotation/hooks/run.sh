#!/bin/sh
# Shim between Claude Code and a hook entry.
#
# Usage: run.sh <entry>      runs hooks/<entry>.py next to this script.
#
# A hook that exits 2 blocks the user's session, so this script never exits 2:
#   - CLAUDE_CONTEXT_ROTATION=off (exactly "off"): exit 0, nothing else happens.
#   - the entry file or python3 is missing: one line on stderr, exit 1.
#   - the entry's own exit status 2 is mapped to 1; every other status passes through.
# hooks.json calls this as `sh run.sh <entry> || exit 1`, so a missing run.sh
# (which dash reports as 2) also ends as 1.

if [ "${CLAUDE_CONTEXT_ROTATION}" = "off" ]; then
  exit 0
fi

entry="$1"
case "$entry" in
  "" | */* | .*)
    echo "context-rotation: invalid hook entry name '$entry'" >&2
    exit 1
    ;;
esac

here=$(dirname "$0")
script="$here/$entry.py"

if [ ! -f "$script" ]; then
  echo "context-rotation: hook entry not found: $script" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "context-rotation: python3 not found, hook skipped" >&2
  exit 1
fi

python3 "$script"
status=$?
if [ "$status" -eq 2 ]; then
  exit 1
fi
exit "$status"
