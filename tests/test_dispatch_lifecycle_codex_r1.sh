#!/usr/bin/env bash
# Regression tests for codex round-1 findings on dispatch_lifecycle.sh (PR #315).
#
# Finding 1 (lines 233-240): _call_cleanup_worker_exit must pass --lease-generation
#   and its value as TWO separate argv entries, not a single concatenated token.
#
# Finding 2 (lines 246-267, 286-292): rc_release_on_failure fallback path must pass
#   exit_status="failure" to _call_cleanup_worker_exit, not "success".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFECYCLE="$SCRIPT_DIR/../scripts/lib/dispatch_lifecycle.sh"

PASS=0
FAIL=0

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label (expected='$expected' actual='$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label (expected substring='$needle' not found in='$haystack')"
        FAIL=$((FAIL + 1))
    fi
}

# ── Helpers to extract only the functions under test ─────────────────────────

# Capture just _call_cleanup_worker_exit from lifecycle, with VNX_DIR substitution.
_load_call_cleanup() {
    local vnx_dir="$1"
    # Provide stubs for log/log_structured_failure used elsewhere; not needed here.
    log() { :; }
    export -f log
    VNX_DIR="$vnx_dir"
    # Extract and eval the function body from the source file.
    eval "$(sed -n '/^_call_cleanup_worker_exit/,/^}/p' "$LIFECYCLE")"
}

# ── Test 1: Finding 1 — --lease-generation arrives as two argv entries ────────
(
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT

    # Mock cleanup_worker_exit.py: write all argv to a JSON array file.
    MOCK_SCRIPT="$TMP/scripts/lib/cleanup_worker_exit.py"
    mkdir -p "$TMP/scripts/lib"
    cat > "$MOCK_SCRIPT" <<'PYEOF'
import sys, json
with open(sys.argv[1], "w") as f:
    json.dump(sys.argv[2:], f)
PYEOF

    # We need the first positional arg to be the output file.
    # Wrap: our mock writes to a fixed path, uses $1 as the target.
    ARGV_FILE="$TMP/argv.json"
    cat > "$MOCK_SCRIPT" <<PYEOF
import sys, json
with open("$ARGV_FILE", "w") as f:
    json.dump(sys.argv[1:], f)
PYEOF

    # Load just _call_cleanup_worker_exit with VNX_DIR=$TMP
    VNX_DIR="$TMP"
    eval "$(sed -n '/^_call_cleanup_worker_exit/,/^}/p' "$LIFECYCLE")"

    # Call with generation=3
    _call_cleanup_worker_exit "T1" "d-test-001" "success" "3"

    if [[ ! -f "$ARGV_FILE" ]]; then
        echo "FAIL: Finding1 - mock was not invoked (argv file missing)"
        exit 1
    fi

    # --lease-generation must be a separate entry from "3"
    ARGS="$(cat "$ARGV_FILE")"
    # The array must contain "--lease-generation" as its own element
    if python3 -c "
import json, sys
args = json.load(open('$ARGV_FILE'))
assert '--lease-generation' in args, f'--lease-generation not in args: {args}'
idx = args.index('--lease-generation')
assert args[idx+1] == '3', f'generation value not at idx+1: {args}'
print('ok')
" 2>/dev/null | grep -q ok; then
        echo "PASS: Finding1 - --lease-generation passed as two separate argv tokens"
        exit 0
    else
        echo "FAIL: Finding1 - --lease-generation NOT passed as separate argv tokens"
        cat "$ARGV_FILE"
        exit 1
    fi
)
RET=$?
if [[ $RET -eq 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi

# ── Test 2: Finding 1 — no --lease-generation when generation is empty ────────
(
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT

    ARGV_FILE="$TMP/argv.json"
    mkdir -p "$TMP/scripts/lib"
    cat > "$TMP/scripts/lib/cleanup_worker_exit.py" <<PYEOF
import sys, json
with open("$ARGV_FILE", "w") as f:
    json.dump(sys.argv[1:], f)
PYEOF

    VNX_DIR="$TMP"
    eval "$(sed -n '/^_call_cleanup_worker_exit/,/^}/p' "$LIFECYCLE")"

    _call_cleanup_worker_exit "T1" "d-test-002" "success" ""

    if python3 -c "
import json
args = json.load(open('$ARGV_FILE'))
assert '--lease-generation' not in args, f'--lease-generation should be absent: {args}'
print('ok')
" 2>/dev/null | grep -q ok; then
        echo "PASS: Finding1 - no --lease-generation emitted when generation is empty"
        exit 0
    else
        echo "FAIL: Finding1 - unexpected --lease-generation when generation is empty"
        cat "$ARGV_FILE"
        exit 1
    fi
)
RET=$?
if [[ $RET -eq 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi

# ── Test 3: Finding 2 — rc_release_lease forwards dispatch_exit_status ────────
(
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT

    CALLS_FILE="$TMP/calls.txt"

    # Stub all bash functions called by rc_release_lease.
    log()                    { :; }
    log_structured_failure() { :; }
    emit_lease_cleanup_audit() { :; }
    _rc_enabled()            { return 0; }
    # _rc_python release-lease: succeed.
    _rc_python()             { return 0; }
    # _call_cleanup_worker_exit: record exit_status arg.
    _call_cleanup_worker_exit() {
        # $3 is exit_status
        echo "$3" >> "$CALLS_FILE"
    }
    export -f log log_structured_failure emit_lease_cleanup_audit \
               _rc_enabled _rc_python _call_cleanup_worker_exit

    VNX_DIR="$TMP"
    eval "$(sed -n '/^rc_release_lease/,/^}/p' "$LIFECYCLE")"

    # Call with explicit dispatch_exit_status="failure"
    rc_release_lease "T1" "5" "d-test-003" "failure" || true

    RECORDED="$(cat "$CALLS_FILE" 2>/dev/null || echo '')"
    if [[ "$RECORDED" == "failure" ]]; then
        echo "PASS: Finding2 - rc_release_lease forwards dispatch_exit_status=failure"
        exit 0
    else
        echo "FAIL: Finding2 - rc_release_lease exit_status was '$RECORDED', expected 'failure'"
        exit 1
    fi
)
RET=$?
if [[ $RET -eq 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi

# ── Test 4: Finding 2 — rc_release_lease defaults to "success" ────────────────
(
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT

    CALLS_FILE="$TMP/calls.txt"

    log()                    { :; }
    log_structured_failure() { :; }
    emit_lease_cleanup_audit() { :; }
    _rc_enabled()            { return 0; }
    _rc_python()             { return 0; }
    _call_cleanup_worker_exit() { echo "$3" >> "$CALLS_FILE"; }
    export -f log log_structured_failure emit_lease_cleanup_audit \
               _rc_enabled _rc_python _call_cleanup_worker_exit

    VNX_DIR="$TMP"
    eval "$(sed -n '/^rc_release_lease/,/^}/p' "$LIFECYCLE")"

    # Call WITHOUT dispatch_exit_status — must default to "success"
    rc_release_lease "T1" "5" "d-test-004" || true

    RECORDED="$(cat "$CALLS_FILE" 2>/dev/null || echo '')"
    if [[ "$RECORDED" == "success" ]]; then
        echo "PASS: Finding2 - rc_release_lease defaults dispatch_exit_status to success"
        exit 0
    else
        echo "FAIL: Finding2 - rc_release_lease default exit_status was '$RECORDED', expected 'success'"
        exit 1
    fi
)
RET=$?
if [[ $RET -eq 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
