#!/usr/bin/env bash
# Tests for _pdp_resolve_target() subprocess pre-flight skip
# Gate: dispatcher-skip-tmux-preflight-for-subprocess
#
# Covers:
#   1. T1 default (no env var) → subprocess adapter → skips tmux pre-flight
#   2. T1 with VNX_ADAPTER_T1=subprocess → skips tmux pre-flight
#   3. T2 with VNX_ADAPTER_T2=subprocess → skips tmux pre-flight
#   4. T2 default (no env var) → tmux adapter → runs tmux pre-flight
#   5. T1 with VNX_ADAPTER_T1=tmux → explicit tmux override → runs tmux pre-flight
#   6. mode_pre_check is called for subprocess terminals (sets _CTM_* globals)
#   7. mode_pre_check failure blocks dispatch for subprocess terminal

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Test harness ---
PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

assert_pass() {
    local rc="$1" msg="$2"
    if [ "$rc" -eq 0 ]; then pass "$msg"; else fail "$msg" "expected exit 0, got $rc"; fi
}

assert_fail() {
    local rc="$1" msg="$2"
    if [ "$rc" -ne 0 ]; then pass "$msg"; else fail "$msg" "expected non-zero exit, got 0"; fi
}

assert_file_contains() {
    local file="$1" pattern="$2" msg="$3"
    if grep -q "$pattern" "$file" 2>/dev/null; then pass "$msg"; else fail "$msg" "pattern '$pattern' not in call log"; fi
}

assert_file_not_contains() {
    local file="$1" pattern="$2" msg="$3"
    if ! grep -q "$pattern" "$file" 2>/dev/null; then pass "$msg"; else fail "$msg" "unexpected pattern '$pattern' found in call log"; fi
}

# --- Temp environment ---
TMP_ROOT=$(mktemp -d)
CALL_LOG="$TMP_ROOT/call.log"
STATE_DIR="$TMP_ROOT/state"
VNX_DATA_DIR="$TMP_ROOT/vnx-data"
VNX_DIR="$PROJECT_ROOT"
mkdir -p "$STATE_DIR" "$VNX_DATA_DIR/unified_reports"
export STATE_DIR VNX_DATA_DIR VNX_DIR
touch "$CALL_LOG"

# --- Mock dispatch file ---
DISPATCH_FILE="$TMP_ROOT/test_dispatch.md"
cat > "$DISPATCH_FILE" <<'DISPATCH_EOF'
---
Track: A
Role: backend-developer
---
Instruction:
Test dispatch instruction.
[[DONE]]
DISPATCH_EOF

# --- Panes registry ---
cat > "$STATE_DIR/panes.json" <<'EOF'
{"T1": "%10", "T2": "%11", "T3": "%12"}
EOF

# --- Global stubs (required by dispatch_create.sh) ---
# _CTM_* globals required by mode_pre_check / configure_terminal_mode
_CTM_TERMINAL_ID="" _CTM_PROVIDER="" _CTM_MODE="" _CTM_CLEAR_CONTEXT=""
_CTM_REQUIRES_MODEL="" _CTM_REQUIRES_MODEL_STRENGTH="" _CTM_FORCE_NORMAL=""
_CTM_REQUIRES_PROVIDER="" _CTM_REQUIRES_PROVIDER_STRENGTH=""

log()                         { :; }
log_structured_failure()      { :; }
sleep()                       { :; }
vnx_dispatch_extract_requires_mcp() { echo "false"; }
get_terminal_provider()       { echo "claude_code"; }
extract_mode()                { echo "normal"; }
extract_clear_context()       { echo "false"; }
extract_requires_model()      { echo "sonnet"; }
extract_requires_provider()   { echo ""; }

# Pane-to-terminal mapping
get_pane_id() { :; }
get_terminal_from_pane() {
    local pane_id="$1"
    case "$pane_id" in
        "%10") echo "T1"; return 0 ;;
        "%11") echo "T2"; return 0 ;;
        "%12") echo "T3"; return 0 ;;
        *) return 1 ;;
    esac
}

# track_to_terminal (mirrors dispatch_lifecycle.sh)
track_to_terminal() {
    case "$1" in
        A) echo "T1" ;;
        B) echo "T2" ;;
        C) echo "T3" ;;
        *) echo "" ;;
    esac
}

# determine_executor returns pane for track
determine_executor() {
    local track="$1"
    case "$track" in
        A) echo "%10" ;;
        B) echo "%11" ;;
        C) echo "%12" ;;
        *) echo "%10" ;;
    esac
}

# --- Call-tracked mocks ---
reset_call_log() {
    > "$CALL_LOG"
    unset VNX_ADAPTER_T1 VNX_ADAPTER_T2 VNX_ADAPTER_T3 2>/dev/null || true
    _CTM_REQUIRES_MODEL=""
    _MODE_PRE_CHECK_FAIL=0
}

# Tracks call; always returns normal (0:0:)
_input_mode_probe() {
    echo "_input_mode_probe $*" >> "$CALL_LOG"
    printf '0:0:'
    return 0
}

# Tracks call; succeeds by default
configure_terminal_mode() {
    echo "configure_terminal_mode $*" >> "$CALL_LOG"
    return 0
}

# Tracks call; sets _CTM_REQUIRES_MODEL; respects _MODE_PRE_CHECK_FAIL
_MODE_PRE_CHECK_FAIL=0
mode_pre_check() {
    echo "mode_pre_check $*" >> "$CALL_LOG"
    if [[ "${_MODE_PRE_CHECK_FAIL:-0}" -eq 1 ]]; then
        return 1
    fi
    _CTM_REQUIRES_MODEL="sonnet"
    return 0
}

# Tracks call
tmux_send_best_effort() {
    echo "tmux_send_best_effort $*" >> "$CALL_LOG"
    return 0
}

# Source the library under test
source "$PROJECT_ROOT/scripts/lib/dispatch_create.sh"

# ===========================================================================
# Test 1: T1 default (no VNX_ADAPTER_T1) → subprocess adapter → skips pre-flight
# ===========================================================================
echo ""
echo "=== Test 1: T1 default → subprocess → skips tmux pre-flight ==="

reset_call_log
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "A" "backend-developer" || rc=$?
assert_pass $rc "T1 default: _pdp_resolve_target succeeds"
assert_file_not_contains "$CALL_LOG" "_input_mode_probe"  "T1 default: _input_mode_probe NOT called"
assert_file_not_contains "$CALL_LOG" "configure_terminal_mode" "T1 default: configure_terminal_mode NOT called"
assert_file_not_contains "$CALL_LOG" "tmux_send_best_effort" "T1 default: tmux_send_best_effort (C-u) NOT called"
assert_file_contains     "$CALL_LOG" "mode_pre_check"     "T1 default: mode_pre_check IS called"

# ===========================================================================
# Test 2: T1 with VNX_ADAPTER_T1=subprocess → subprocess adapter → skips pre-flight
# ===========================================================================
echo ""
echo "=== Test 2: VNX_ADAPTER_T1=subprocess → skips tmux pre-flight ==="

reset_call_log
export VNX_ADAPTER_T1=subprocess
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "A" "backend-developer" || rc=$?
assert_pass $rc "T1 explicit subprocess: _pdp_resolve_target succeeds"
assert_file_not_contains "$CALL_LOG" "_input_mode_probe"  "T1 subprocess: _input_mode_probe NOT called"
assert_file_not_contains "$CALL_LOG" "configure_terminal_mode" "T1 subprocess: configure_terminal_mode NOT called"
assert_file_contains     "$CALL_LOG" "mode_pre_check"     "T1 subprocess: mode_pre_check IS called"
unset VNX_ADAPTER_T1

# ===========================================================================
# Test 3: T2 with VNX_ADAPTER_T2=subprocess → subprocess adapter → skips pre-flight
# ===========================================================================
echo ""
echo "=== Test 3: VNX_ADAPTER_T2=subprocess → skips tmux pre-flight ==="

reset_call_log
export VNX_ADAPTER_T2=subprocess
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "B" "backend-developer" || rc=$?
assert_pass $rc "T2 subprocess: _pdp_resolve_target succeeds"
assert_file_not_contains "$CALL_LOG" "_input_mode_probe"  "T2 subprocess: _input_mode_probe NOT called"
assert_file_not_contains "$CALL_LOG" "configure_terminal_mode" "T2 subprocess: configure_terminal_mode NOT called"
assert_file_contains     "$CALL_LOG" "mode_pre_check"     "T2 subprocess: mode_pre_check IS called"
unset VNX_ADAPTER_T2

# ===========================================================================
# Test 4: T2 default (no env var) → tmux adapter → runs tmux pre-flight
# ===========================================================================
echo ""
echo "=== Test 4: T2 default → tmux → runs tmux pre-flight ==="

reset_call_log
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "B" "backend-developer" || rc=$?
assert_pass $rc "T2 default tmux: _pdp_resolve_target succeeds"
assert_file_contains     "$CALL_LOG" "_input_mode_probe"  "T2 tmux: _input_mode_probe IS called"
assert_file_contains     "$CALL_LOG" "configure_terminal_mode" "T2 tmux: configure_terminal_mode IS called"
assert_file_contains     "$CALL_LOG" "tmux_send_best_effort"   "T2 tmux: tmux_send_best_effort (C-u) IS called"
assert_file_not_contains "$CALL_LOG" "mode_pre_check"     "T2 tmux: mode_pre_check NOT called directly (called via configure_terminal_mode)"

# ===========================================================================
# Test 5: T1 with VNX_ADAPTER_T1=tmux → explicit tmux override → runs pre-flight
# ===========================================================================
echo ""
echo "=== Test 5: VNX_ADAPTER_T1=tmux → explicit tmux override → runs pre-flight ==="

reset_call_log
export VNX_ADAPTER_T1=tmux
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "A" "backend-developer" || rc=$?
assert_pass $rc "T1 explicit tmux: _pdp_resolve_target succeeds"
assert_file_contains     "$CALL_LOG" "_input_mode_probe"  "T1 tmux override: _input_mode_probe IS called"
assert_file_contains     "$CALL_LOG" "configure_terminal_mode" "T1 tmux override: configure_terminal_mode IS called"
unset VNX_ADAPTER_T1

# ===========================================================================
# Test 6: _CTM_REQUIRES_MODEL set after subprocess pre-flight
# ===========================================================================
echo ""
echo "=== Test 6: _CTM_REQUIRES_MODEL is populated after subprocess path ==="

reset_call_log
_CTM_REQUIRES_MODEL=""
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "A" "backend-developer" || rc=$?
assert_pass $rc "T1 subprocess: succeeds"
if [ "$_CTM_REQUIRES_MODEL" = "sonnet" ]; then
    pass "T1 subprocess: _CTM_REQUIRES_MODEL=sonnet set by mode_pre_check"
else
    fail "T1 subprocess: _CTM_REQUIRES_MODEL not set" "got='$_CTM_REQUIRES_MODEL'"
fi

# ===========================================================================
# Test 7: mode_pre_check failure blocks dispatch for subprocess terminal
# ===========================================================================
echo ""
echo "=== Test 7: mode_pre_check failure → dispatch blocked for subprocess ==="

reset_call_log
_MODE_PRE_CHECK_FAIL=1
rc=0; _pdp_resolve_target "$DISPATCH_FILE" "A" "backend-developer" || rc=$?
assert_fail $rc "T1 subprocess: mode_pre_check failure → _pdp_resolve_target returns 1"
assert_file_contains "$CALL_LOG" "mode_pre_check" "T1 subprocess: mode_pre_check was called before failure"
_MODE_PRE_CHECK_FAIL=0

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "=== Results ==="
echo "PASS: $PASS_COUNT"
echo "FAIL: $FAIL_COUNT"
echo ""

# Cleanup
rm -rf "$TMP_ROOT"

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "RESULT: FAIL ($FAIL_COUNT test(s) failed)"
    exit 1
else
    echo "RESULT: PASS — all $PASS_COUNT dispatcher subprocess pre-flight skip tests passed"
    exit 0
fi
