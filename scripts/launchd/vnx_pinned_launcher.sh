#!/usr/bin/env bash
# vnx_pinned_launcher.sh — start one launchd job on its project's pinned engine.
#
# Usage: vnx_pinned_launcher.sh <project_id> <relative-script> [args...]
#
# Every start resolves again. The resolver (scripts/lib/pinned_engine.py) comes from the
# tree THIS launcher lives in — usually the rolling "current" install a release ships —
# so a newer tree starts an older pin; nothing is imported from the target tree, which
# must only carry <relative-script>. A run in progress is never switched: it finishes on
# the tree it started from, and only the NEXT launchd start picks up a changed pin.
#
# No pin or a resolvable pin -> that tree. A dead pin, an unreadable/malformed/escaping
# pin, an unregistered project or a pinned tree without <relative-script> -> exit 78
# (EX_CONFIG) with one stderr line from the resolver. There is no fallback to another
# tree, ever.
#
# Undo: revert the commit that pointed a plist at this launcher and run
# `bash scripts/launchd/reload_plist.sh <template>`, which restores the direct call.

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $(basename "$0") <project_id> <relative-script> [args...]" >&2
    exit 78
fi
PROJECT_ID="$1"
RELATIVE_SCRIPT="$2"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOLVER="$SCRIPT_DIR/../lib/pinned_engine.py"
if [ ! -f "$RESOLVER" ]; then
    echo "ERROR: pinned-engine resolver not found: $RESOLVER" >&2
    exit 78
fi
# The tree this launcher was installed from: VNX_HOME as launchd sets it, else its ../..
DECLARED_HOME="${VNX_HOME:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Prints the engine root, or exits 78 having printed its one line (dead pin, unregistered
# project, or a chosen tree without <relative-script>).
if ! ENGINE_ROOT="$(python3 "$RESOLVER" "$PROJECT_ID" "$DECLARED_HOME" "$RELATIVE_SCRIPT")"; then
    exit 78
fi

cd "$ENGINE_ROOT" || {
    echo "ERROR: cannot enter the resolved engine root $ENGINE_ROOT" >&2
    exit 78
}
export VNX_HOME="$ENGINE_ROOT"
# Appended, never prepended (OI-1663): a ~/.local/bin/python3 must not shadow the system
# python3 this job depends on.
export PATH="$PATH:${HOME:?HOME is not set}/.local/bin"

exec python3 "$RELATIVE_SCRIPT" "$@"
