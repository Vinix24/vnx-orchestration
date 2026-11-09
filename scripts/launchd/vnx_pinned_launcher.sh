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
# The interpreter comes from the script's own extension: *.py runs the interpreter
# vnx_python.sh picks for the engine root (inside requires-python), *.sh runs
# bash, anything else exits 78 with one line naming the script.
#
# The script runs as "$ENGINE_ROOT/<relative-script>" with cwd = the project checkout and
# PROJECT_ROOT / VNX_PROJECT_ROOT = that checkout; VNX_HOME stays the engine. A checkout
# that is not a directory exits 78 with one line.
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

# The interpreter is the script's own extension, never a guess: a `*.sh` job (the
# receipt-processor supervisor) cannot be started by python3, and a `*.py` job must not
# be handed to bash. Anything else is refused before a tree is resolved at all.
case "$RELATIVE_SCRIPT" in
    *.py) INTERPRETER=resolve ;;
    *.sh) INTERPRETER=bash ;;
    *)
        echo "ERROR: cannot start $RELATIVE_SCRIPT: only *.py and *.sh scripts are supported" >&2
        exit 78
        ;;
esac

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
if ! RESOLVER_PYTHON="$(bash "$SCRIPT_DIR/vnx_python.sh" "$(cd "$SCRIPT_DIR/../.." && pwd)")"; then
    exit 78
fi
if ! RESOLVED="$("$RESOLVER_PYTHON" "$RESOLVER" --with-checkout "$PROJECT_ID" "$DECLARED_HOME" "$RELATIVE_SCRIPT")"; then
    exit 78
fi
ENGINE_ROOT="$(printf '%s\n' "$RESOLVED" | sed -n '1p')"
PROJECT_CHECKOUT="$(printf '%s\n' "$RESOLVED" | sed -n 's/^checkout=//p')"

# A project-bound daemon runs in its project checkout, never in the engine tree: that tree
# is a read-only release, so a root resolved from it makes vnx_paths refuse and a git call
# there cannot write .git/FETCH_HEAD (OI-2653, OI-2616). No fallback to the engine tree.
if [ -z "$PROJECT_CHECKOUT" ] || [ ! -d "$PROJECT_CHECKOUT" ]; then
    echo "ERROR: project '$PROJECT_ID' checkout '${PROJECT_CHECKOUT:-<unresolved>}' is not a directory; refusing to start $RELATIVE_SCRIPT" >&2
    exit 78
fi
cd "$PROJECT_CHECKOUT" || {
    echo "ERROR: project '$PROJECT_ID' checkout '$PROJECT_CHECKOUT' cannot be entered; refusing to start $RELATIVE_SCRIPT" >&2
    exit 78
}
export VNX_HOME="$ENGINE_ROOT"
# PROJECT_ROOT is what the shell and python path helpers read; VNX_PROJECT_ROOT is the
# explicit override vnx_paths honours before any heuristic.
export PROJECT_ROOT="$PROJECT_CHECKOUT"
export VNX_PROJECT_ROOT="$PROJECT_CHECKOUT"
# Appended, never prepended (OI-1663): a ~/.local/bin/python3 must not shadow the system
# python3 this job depends on.
export PATH="$PATH:${HOME:?HOME is not set}/.local/bin"

if [ "$INTERPRETER" = resolve ]; then
    if ! INTERPRETER="$(bash "$SCRIPT_DIR/vnx_python.sh" "$ENGINE_ROOT")"; then
        exit 78
    fi
fi

exec "$INTERPRETER" "$ENGINE_ROOT/$RELATIVE_SCRIPT" "$@"
