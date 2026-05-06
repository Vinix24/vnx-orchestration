#!/usr/bin/env bash
# tests/test_dispatch_project_guard.sh — OI-1316 hardening tests.
#
# Two new cases for Phase 1.5 PR-3:
#   TC1: symlink-traversal on non-existent path — guard must reject
#   TC2: unstamped dispatch quarantine          — guard must reject + move file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GUARD_SH="$REPO_ROOT/scripts/lib/dispatch_project_guard.sh"
META_SH="$REPO_ROOT/scripts/lib/dispatch_metadata.sh"

pass=0
fail=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" != "$actual" ]; then
        echo "FAIL: $msg (expected='$expected' actual='$actual')"
        fail=$((fail + 1))
    else
        echo "PASS: $msg"
        pass=$((pass + 1))
    fi
}

assert_file_exists() {
    local path="$1" msg="$2"
    if [ -e "$path" ]; then
        echo "PASS: $msg"
        pass=$((pass + 1))
    else
        echo "FAIL: $msg (file not found: $path)"
        fail=$((fail + 1))
    fi
}

assert_file_missing() {
    local path="$1" msg="$2"
    if [ ! -e "$path" ]; then
        echo "PASS: $msg"
        pass=$((pass + 1))
    else
        echo "FAIL: $msg (file should not exist: $path)"
        fail=$((fail + 1))
    fi
}

assert_file_contains() {
    local path="$1" needle="$2" msg="$3"
    if grep -qF "$needle" "$path" 2>/dev/null; then
        echo "PASS: $msg"
        pass=$((pass + 1))
    else
        echo "FAIL: $msg (pattern not found in $path)"
        fail=$((fail + 1))
    fi
}

# ---------------------------------------------------------------------------
# TC1: symlink-traversal on non-existent path
#
# Attack: VNX_DATA_DIR is set to a legit dir, but the child path uses "../other"
# to escape it. The guard must canonicalize the non-existent path through its
# deepest existing ancestor and reject when the resolved path escapes VNX_DATA_DIR.
# ---------------------------------------------------------------------------

tc1_tmpdir="$(mktemp -d)"
trap 'rm -rf "$tc1_tmpdir"' EXIT

legit_vnx_data="$tc1_tmpdir/legit/.vnx-data"
mkdir -p "$legit_vnx_data"

# Attack path: starts with legit/.vnx-data/ but escapes via "../other/"
attack_child="$legit_vnx_data/../other/.vnx-data/dispatches"

rc=0
result="$(bash -c "
    set -uo pipefail
    source \"$GUARD_SH\"
    vnx_dispatch_assert_dir_under \"$attack_child\" \"$legit_vnx_data\"; printf '%s\n' \"\$?\"
" 2>/dev/null)" || rc=$?

assert_eq "1" "$result" "TC1: path traversal via non-existent dir is rejected"

# ---------------------------------------------------------------------------
# TC2: unstamped dispatch quarantine
#
# Dispatches written to pending/ without a Project-ID: header must be quarantined
# (moved to rejected_dir with a REJECTED marker) rather than accepted as legacy.
# ---------------------------------------------------------------------------

tc2_tmpdir="$(mktemp -d)"
trap 'rm -rf "$tc1_tmpdir" "$tc2_tmpdir"' EXIT

pending_dir="$tc2_tmpdir/pending"
quarantine_dir="$tc2_tmpdir/quarantine"
mkdir -p "$pending_dir" "$quarantine_dir"

dispatch_file="$pending_dir/unstamped-dispatch.md"
cat > "$dispatch_file" <<'EOF'
[[TARGET:A]]
Manager Block

Role: backend-developer
Track: A
Terminal: T1
Gate: test-gate
Dispatch-ID: test-unstamped-001

Instruction:
noop
[[DONE]]
EOF

status_out="$(bash -c "
    set -uo pipefail
    source \"$META_SH\"
    source \"$GUARD_SH\"
    status=\$(vnx_dispatch_validate_project_id \"$dispatch_file\" \"vnx-dev\" \"$quarantine_dir\")
    printf 'STATUS=%s\n' \"\$status\"
" 2>/dev/null)"

status_val="${status_out#STATUS=}"

assert_eq "reject" "$status_val" "TC2: unstamped dispatch returns reject status"
assert_file_missing "$dispatch_file" "TC2: unstamped dispatch removed from pending/"
assert_file_exists "$quarantine_dir/unstamped-dispatch.md" "TC2: unstamped dispatch moved to quarantine"
assert_file_contains "$quarantine_dir/unstamped-dispatch.md" "[REJECTED: unstamped dispatch]" "TC2: quarantine marker written"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "Results: $pass passed, $fail failed"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
echo "ALL PASS: test_dispatch_project_guard.sh"
