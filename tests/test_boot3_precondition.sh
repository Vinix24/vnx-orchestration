#!/usr/bin/env bash
# Regression test: BOOT-3 fail-closed check must fire BEFORE any mkdir -p
# creates .vnx-data directories, so an unbootstrapped session is rejected
# before the dispatcher can silently bootstrap a fresh runtime directory.
#
# Finding: scripts/dispatcher_v8_minimal.sh BOOT-3 was placed after mkdir -p
# calls that created $VNX_DATA_DIR, defeating the directory-existence check.
# Fix: BOOT-3 moved to immediately after vnx_paths.sh is sourced (line ~18).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCHER="$PROJECT_ROOT/scripts/dispatcher_v8_minimal.sh"

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# Test 1: Unset VNX_DATA_DIR → dispatcher must exit non-zero immediately,
#         before creating any directory under a temp location.
# ---------------------------------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

FAKE_DATA="$TMP/vnx-data-should-not-be-created"

# Run with VNX_DATA_DIR pointing at a non-existent directory (not pre-created).
# The dispatcher must refuse to start — it must NOT silently create the dir.
set +e
output=$(
    env -i \
        HOME="$HOME" \
        PATH="$PATH" \
        VNX_DATA_DIR="$FAKE_DATA" \
        VNX_STATE_DIR="$FAKE_DATA/state" \
        bash "$DISPATCHER" 2>&1
)
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
    pass "dispatcher exits non-zero when VNX_DATA_DIR does not exist (rc=$rc)"
else
    fail "dispatcher should have exited non-zero; got rc=0"
fi

if echo "$output" | grep -q "FATAL"; then
    pass "FATAL message emitted before any runtime init"
else
    fail "expected FATAL message; got: $output"
fi

if [ ! -d "$FAKE_DATA" ]; then
    pass "dispatcher did NOT create \$VNX_DATA_DIR before BOOT-3 fired"
else
    fail "dispatcher created \$VNX_DATA_DIR before BOOT-3 check — early-exit broken"
fi

# ---------------------------------------------------------------------------
# Test 2: VNX_DATA_DIR exists but VNX_STATE_DIR does not → must also reject.
# ---------------------------------------------------------------------------
FAKE_DATA2="$TMP/vnx-data-partial"
mkdir -p "$FAKE_DATA2"   # data dir exists, but state subdir does not

set +e
output2=$(
    env -i \
        HOME="$HOME" \
        PATH="$PATH" \
        VNX_DATA_DIR="$FAKE_DATA2" \
        VNX_STATE_DIR="$FAKE_DATA2/state" \
        bash "$DISPATCHER" 2>&1
)
rc2=$?
set -e

if [ "$rc2" -ne 0 ]; then
    pass "dispatcher exits non-zero when VNX_STATE_DIR does not exist (rc=$rc2)"
else
    fail "dispatcher should have exited non-zero when VNX_STATE_DIR missing; got rc=0"
fi

if echo "$output2" | grep -q "FATAL"; then
    pass "FATAL message emitted for missing VNX_STATE_DIR"
else
    fail "expected FATAL message for missing state dir; got: $output2"
fi

# VNX_STATE_DIR must not have been silently created
if [ ! -d "$FAKE_DATA2/state" ]; then
    pass "dispatcher did NOT create VNX_STATE_DIR before BOOT-3 fired"
else
    fail "dispatcher created VNX_STATE_DIR before BOOT-3 check — early-exit broken"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
