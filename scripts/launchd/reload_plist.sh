#!/usr/bin/env bash
# Generic launchd plist reload helper.
#
# Usage: bash scripts/launchd/reload_plist.sh <name> [project_id]
#   <name>        — plist filename without .plist extension
#                   e.g. com.vnx.conversation-analyzer
#   [project_id]  — optional; only consulted when <name>'s template Label
#                    contains the ${VNX_PROJECT_ID} placeholder (e.g.
#                    com.vnx.gate-obligation-runner, com.vnx.receipt-processor).
#                    Falls back to $VNX_PROJECT_ID, then the nearest
#                    .vnx-project-id marker walking up from $VNX_HOME.
#
# Requires $VNX_HOME to be set, or derives from script location (scripts/launchd/ → repo root).
# Substitutes ${VNX_HOME} (and, for per-project templates, ${VNX_PROJECT_ID}) in the
# plist template, then writes the resolved plist to
# ~/Library/LaunchAgents/<resolved Label>.plist and reloads via launchctl.
#
# OI-1509/OI-1510: the destination filename (and the Label launchd registers)
# is derived from the TEMPLATE'S OWN resolved Label, never from the CLI <name>
# argument. Before this, every caller of this script landed at
# ~/Library/LaunchAgents/<name>.plist regardless of what the template's Label
# actually said — for a per-project template that meant two projects installing
# the SAME template collided on the SAME destination file and the SAME launchd
# Label: the second install's `launchctl unload` silently tore down the first
# project's job before overwriting its file. A per-project template's Label
# now carries ${VNX_PROJECT_ID}, so two projects resolve to two different
# Labels and therefore two different destination files, and never touch each
# other's job.
#
# Returns 0 on success, non-zero on failure.

set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "Usage: $0 <name> [project_id]" >&2
    echo "  <name>       — plist name without .plist (e.g. com.vnx.conversation-analyzer)" >&2
    echo "  [project_id] — only used for per-project templates (Label contains \${VNX_PROJECT_ID})" >&2
    exit 1
fi

TEMPLATE_NAME="$1"
PROJECT_ID_ARG="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/${TEMPLATE_NAME}.plist"

if [ -z "${VNX_HOME:-}" ]; then
    VNX_HOME="$(cd "$SCRIPT_DIR/../.." && pwd)"
    echo "VNX_HOME not set — using derived path: $VNX_HOME"
fi

if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: plist template not found: $PLIST_SRC" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 required (used to read the resolved plist's Label; templates format" >&2
    echo "  the Label key/value pair differently across files, and a real plist parser" >&2
    echo "  handles all of them instead of a fragile single-line regex)." >&2
    exit 1
fi

# Best-effort project_id resolution: explicit arg > $VNX_PROJECT_ID env >
# nearest .vnx-project-id marker walking up from $VNX_HOME. Mirrors
# _vnx_state_project_id in scripts/lib/vnx_paths.sh (kept as a small,
# deliberate duplicate here rather than sourcing the full path resolver,
# which would re-derive VNX_HOME/VNX_DATA_DIR under this script's own
# already-resolved VNX_HOME and risk disagreeing with it).
_resolve_project_id() {
    local start_dir="$1" dir first
    if [ -n "${PROJECT_ID_ARG:-}" ]; then
        printf '%s' "$PROJECT_ID_ARG"
        return 0
    fi
    if [ -n "${VNX_PROJECT_ID:-}" ]; then
        printf '%s' "$VNX_PROJECT_ID"
        return 0
    fi
    dir="$start_dir"
    while [ -n "$dir" ]; do
        if [ -f "$dir/.vnx-project-id" ]; then
            first="$(head -1 "$dir/.vnx-project-id" 2>/dev/null | tr -d '[:space:]')"
            if [ -n "$first" ]; then
                printf '%s' "$first"
            fi
            return 0
        fi
        [ "$dir" = "/" ] && break
        dir="$(dirname "$dir")"
    done
    return 0
}

# Substitute ${VNX_HOME} first; only touch ${VNX_PROJECT_ID} for templates
# that actually declare it, so unrelated templates behave exactly as before.
RESOLVED="$(sed "s|\${VNX_HOME}|$VNX_HOME|g" "$PLIST_SRC")"

if printf '%s' "$RESOLVED" | grep -q '\${VNX_PROJECT_ID}'; then
    PROJECT_ID="$(_resolve_project_id "$VNX_HOME")"
    if [ -z "$PROJECT_ID" ]; then
        echo "ERROR: $TEMPLATE_NAME is a per-project template (its Label contains" >&2
        echo "  \${VNX_PROJECT_ID}) but no project id could be resolved. Pass it as the" >&2
        echo "  second argument, export VNX_PROJECT_ID, or ensure a .vnx-project-id" >&2
        echo "  marker exists at or above \$VNX_HOME ($VNX_HOME)." >&2
        exit 1
    fi
    if ! printf '%s' "$PROJECT_ID" | grep -Eq '^[a-z][a-z0-9-]{1,31}$'; then
        echo "ERROR: resolved project id '$PROJECT_ID' does not match ^[a-z][a-z0-9-]{1,31}\$" \
             "— refusing to install a plist with an invalid project id." >&2
        exit 1
    fi
    RESOLVED="$(printf '%s' "$RESOLVED" | sed "s|\${VNX_PROJECT_ID}|$PROJECT_ID|g")"
    echo "Resolved project id: $PROJECT_ID"
fi

# Fail loud on any remaining unresolved ${...} placeholder rather than
# install a plist that will confuse the daemon it starts.
if printf '%s' "$RESOLVED" | grep -qE '\$\{[A-Za-z_][A-Za-z0-9_]*\}'; then
    echo "ERROR: unresolved placeholder(s) remain in $TEMPLATE_NAME after substitution:" >&2
    printf '%s' "$RESOLVED" | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' | sort -u >&2
    exit 1
fi

RESOLVED_LABEL="$(printf '%s' "$RESOLVED" | python3 -c '
import plistlib
import sys

data = plistlib.loads(sys.stdin.buffer.read())
label = data.get("Label")
if not isinstance(label, str) or not label:
    sys.exit(1)
sys.stdout.write(label)
')" || {
    echo "ERROR: could not read a Label out of the resolved $TEMPLATE_NAME plist." >&2
    exit 1
}

PLIST_DEST="$HOME/Library/LaunchAgents/${RESOLVED_LABEL}.plist"

# Unload existing agent if present (ignore errors — may not be loaded yet).
# Unloading by DEST PATH, not by a guessed label: this only ever tears down
# whatever job THIS destination file currently holds, which — now that the
# destination is derived from the resolved Label — is always this project's
# own prior instance, never another project's.
if [ -f "$PLIST_DEST" ]; then
    echo "Unloading existing agent: $RESOLVED_LABEL"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

echo "Writing resolved plist to: $PLIST_DEST"
printf '%s\n' "$RESOLVED" > "$PLIST_DEST"

launchctl load "$PLIST_DEST"
echo "Loaded: $RESOLVED_LABEL"

# Verify the agent appears in launchctl list
if launchctl list | grep -qF "$RESOLVED_LABEL"; then
    echo "OK: agent registered in launchctl"
else
    echo "WARNING: agent not found in launchctl list — check $PLIST_DEST" >&2
    exit 1
fi
