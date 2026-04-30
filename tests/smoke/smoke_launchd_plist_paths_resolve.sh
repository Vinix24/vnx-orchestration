#!/usr/bin/env bash
# Smoke test: verify that every ProgramArguments path in scripts/launchd/*.plist resolves.
#
# Catches broken plist references before launchd silently fails with exit 32512.
# Per audit (claudedocs/2026-04-30-vnx-ci-test-plan.md §"First-flag test"):
# this test would have caught the conversation_analyzer SEOcrawler_v2 bug 19 days earlier.
#
# Usage: bash tests/smoke/smoke_launchd_plist_paths_resolve.sh
# Exit 0: all paths resolve. Exit 1: one or more paths are missing.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VNX_HOME="${VNX_HOME:-$REPO_ROOT}"
PLIST_DIR="$REPO_ROOT/scripts/launchd"

PASS=0
FAIL=0
SKIP=0

check_plist_paths() {
    local plist="$1"
    local label
    label="$(basename "$plist")"

    # Use python3 plistlib to extract ProgramArguments strings reliably
    local args
    args="$(python3 - "$plist" <<'PYEOF'
import sys, plistlib, pathlib

plist_path = sys.argv[1]
with open(plist_path, "rb") as f:
    data = plistlib.load(f)

args = data.get("ProgramArguments", [])
for arg in args:
    print(arg)
PYEOF
)"

    while IFS= read -r arg; do
        [ -z "$arg" ] && continue

        # Skip template placeholders that have not been substituted yet
        if [[ "$arg" == *"__"* ]]; then
            echo "  SKIP  [$label] template placeholder: $arg"
            SKIP=$((SKIP + 1))
            continue
        fi

        # Substitute ${VNX_HOME} with actual value
        local resolved="${arg//\$\{VNX_HOME\}/$VNX_HOME}"

        # Only check absolute paths (ignore flags, option values, etc.)
        if [[ "$resolved" != /* ]]; then
            continue
        fi

        if [ -e "$resolved" ]; then
            echo "  OK    [$label] $resolved"
            PASS=$((PASS + 1))
        else
            echo "  FAIL  [$label] path not found: $resolved" >&2
            FAIL=$((FAIL + 1))
        fi
    done <<< "$args"
}

echo "Scanning $PLIST_DIR/*.plist (VNX_HOME=$VNX_HOME)"
echo ""

shopt -s nullglob
plist_files=("$PLIST_DIR"/*.plist)
shopt -u nullglob

if [ ${#plist_files[@]} -eq 0 ]; then
    echo "ERROR: no plist files found in $PLIST_DIR" >&2
    exit 1
fi

for plist in "${plist_files[@]}"; do
    check_plist_paths "$plist"
done

echo ""
echo "Results: $PASS ok, $FAIL failed, $SKIP skipped (unsubstituted templates)"

if [ "$FAIL" -gt 0 ]; then
    echo "FAIL: $FAIL missing path(s) — fix broken plist references before loading agents" >&2
    exit 1
fi

echo "PASS: all resolved paths exist"
exit 0
