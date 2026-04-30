#!/usr/bin/env bash
# Liveness smoke test: verify launchd agents are registered and last-exited cleanly.
#
# Only runs when VNX_LAUNCHD_LIVE_CHECK=1 — skipped by default so CI never blocks.
# Also skipped automatically when launchctl is absent (Linux CI).
#
# Usage: VNX_LAUNCHD_LIVE_CHECK=1 bash tests/smoke/smoke_launchd_active_jobs.sh
# Exit 0: all agents registered and LastExitStatus=0.
# Exit 1: any agent missing or LastExitStatus non-zero (file-not-found 32512, cmd-not-found 127, etc.)

set -uo pipefail

if [ "${VNX_LAUNCHD_LIVE_CHECK:-0}" != "1" ]; then
    echo "SKIP: set VNX_LAUNCHD_LIVE_CHECK=1 to run live launchd checks"
    exit 0
fi

if ! command -v launchctl &>/dev/null; then
    echo "SKIP: launchctl not available (not macOS or not in PATH)"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_DIR="$REPO_ROOT/scripts/launchd"

PASS=0
FAIL=0

shopt -s nullglob
plist_files=("$PLIST_DIR"/*.plist)
shopt -u nullglob

if [ ${#plist_files[@]} -eq 0 ]; then
    echo "ERROR: no plist files found in $PLIST_DIR" >&2
    exit 1
fi

echo "Checking launchd agents for plists in $PLIST_DIR"
echo ""

for plist in "${plist_files[@]}"; do
    label="$(python3 - "$plist" <<'PYEOF'
import sys, plistlib
with open(sys.argv[1], "rb") as f:
    data = plistlib.load(f)
print(data.get("Label", ""))
PYEOF
)"
    if [ -z "$label" ]; then
        echo "  SKIP  $(basename "$plist") — no Label key found"
        continue
    fi

    list_output="$(launchctl list "$label" 2>/dev/null || true)"
    if [ -z "$list_output" ]; then
        echo "  FAIL  [$label] not registered in launchctl" >&2
        FAIL=$((FAIL + 1))
        continue
    fi

    last_exit="$(echo "$list_output" | grep -m1 '"LastExitStatus"' | grep -oE '[0-9]+' | head -1 || echo "")"
    if [ "${last_exit:-}" = "0" ]; then
        echo "  OK    [$label] LastExitStatus=0"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  [$label] LastExitStatus=${last_exit:-unknown} (expected 0)" >&2
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "Results: $PASS ok, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
    echo "FAIL: $FAIL agent(s) not clean" >&2
    exit 1
fi

echo "PASS: all registered agents clean"
exit 0
