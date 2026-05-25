#!/usr/bin/env bash
# check_env_isolation.sh — Detect cross-repo VNX env contamination.
#
# Checks whether VNX_* env vars in the current shell belong to a different
# project than the current working directory. Prints a WARNING table per
# leaked var and outputs actionable unset commands.
#
# Exit codes:
#   0 — no env leakage detected (clean)
#   1 — one or more VNX_* vars appear to come from a different project
#
# Usage:
#   bash scripts/check_env_isolation.sh
#
# No external dependencies — bash only (no Python, no jq, no awk).
# Compatible with bash 3.2+ (macOS default).

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BOLD='\033[1m'
RESET='\033[0m'

# Detect terminal color support; disable if not a TTY or $NO_COLOR set.
if [[ ! -t 1 ]] || [[ -n "${NO_COLOR:-}" ]]; then
    RED='' YELLOW='' GREEN='' BOLD='' RESET=''
fi

# ---------------------------------------------------------------------------
# Determine current project root (git-based, with fallback to $PWD)
# ---------------------------------------------------------------------------

if git rev-parse --show-toplevel >/dev/null 2>&1; then
    CURRENT_ROOT="$(git rev-parse --show-toplevel)"
else
    CURRENT_ROOT="$PWD"
fi

CURRENT_PROJECT="$(basename "$CURRENT_ROOT")"

# ---------------------------------------------------------------------------
# VNX_* vars to inspect
# ---------------------------------------------------------------------------

VNX_VARS=(
    VNX_DATA_DIR
    VNX_HOME
    VNX_STATE_DIR
    VNX_SKILLS_DIR
    VNX_PROJECT_ID
    VNX_PROJECT_ROOT
)

# ---------------------------------------------------------------------------
# Scan for leakage
# ---------------------------------------------------------------------------

LEAKED_VARS=()
HEADER_PRINTED=0

print_header() {
    if [[ "$HEADER_PRINTED" -eq 0 ]]; then
        echo ""
        echo -e "${BOLD}VNX Environment Isolation Check${RESET}"
        echo "  Current project : ${CURRENT_PROJECT}"
        echo "  Project root    : ${CURRENT_ROOT}"
        echo ""
        printf "  %-25s %-50s %s\n" "Variable" "Current value" "Status"
        printf "  %-25s %-50s %s\n" "-------------------------" "--------------------------------------------------" "--------"
        HEADER_PRINTED=1
    fi
}

for var in "${VNX_VARS[@]}"; do
    val="${!var:-}"

    if [[ -z "$val" ]]; then
        # Not set — no leakage possible.
        continue
    fi

    # Determine whether the value looks like it belongs to the current project.
    # Strategy: value must contain the current project name or current root as
    # a path component. Both absolute-path vars and plain string vars (like
    # VNX_PROJECT_ID) are handled.
    LOOKS_FOREIGN=0

    # Only flag absolute paths (starting with '/') that point to a different
    # project root. Relative paths, IDs, and short names are always considered
    # project-local — the real cross-repo contamination risk comes exclusively
    # from absolute paths inherited from a different tmux pane.
    if [[ "$val" == /* ]]; then
        # Normalise to remove trailing slashes before comparing.
        val_norm="${val%/}"
        root_norm="${CURRENT_ROOT%/}"
        if [[ "$val_norm" != "$root_norm"* ]]; then
            LOOKS_FOREIGN=1
        fi
    fi
    # Non-absolute values (.vnx-data, .vnx-data/state, project-ids, etc.)
    # are left with LOOKS_FOREIGN=0.

    print_header

    if [[ "$LOOKS_FOREIGN" -eq 1 ]]; then
        # Truncate long values for display.
        display_val="$val"
        if [[ "${#display_val}" -gt 50 ]]; then
            display_val="${display_val:0:47}..."
        fi
        printf "  ${YELLOW}%-25s${RESET} %-50s ${RED}LEAKED${RESET}\n" "$var" "$display_val"
        LEAKED_VARS+=("$var")
    else
        display_val="$val"
        if [[ "${#display_val}" -gt 50 ]]; then
            display_val="${display_val:0:47}..."
        fi
        printf "  ${GREEN}%-25s${RESET} %-50s ${GREEN}ok${RESET}\n" "$var" "$display_val"
    fi
done

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

if [[ "${#LEAKED_VARS[@]}" -eq 0 ]]; then
    echo -e "${GREEN}[ok] No VNX env leakage detected — shell is clean for project: ${CURRENT_PROJECT}${RESET}"
    exit 0
fi

echo ""
echo -e "${RED}${BOLD}[WARN] Env leakage detected: ${#LEAKED_VARS[@]} VNX_* var(s) appear to come from a different project.${RESET}"
echo ""
echo "  Risk: migrator may read the wrong state directory, producing incorrect"
echo "  behaviour or silently skipping the intended central DB."
echo ""
echo -e "${BOLD}  Run the following before every migrator apply:${RESET}"
echo ""

# Build a single unset line for easy copy-paste.
UNSET_CMD="unset"
for var in "${LEAKED_VARS[@]}"; do
    UNSET_CMD="$UNSET_CMD $var"
done

echo "    $UNSET_CMD"
echo ""
echo "  Then verify with:"
echo "    bash scripts/check_env_isolation.sh"
echo ""

exit 1
