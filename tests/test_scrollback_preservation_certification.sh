#!/usr/bin/env bash
# Certification: Scrollback Preservation During Retry Storms (PR-1)
# Gate: gate_pr1_scrollback_certification
#
# Certifies that the rate-limited input-mode guard:
#   1. Preserves scrollback during retry storm (N rapid retries, no cancel sent)
#   2. Delivers full recovery on first attempt after cooldown
#   3. Blocks delivery into copy-mode when recovery fails (dispatch-time safety)
#   4. Produces complete audit evidence for all scenarios
#
# Goes beyond PR-0 unit tests by:
#   - Simulating a realistic 10-iteration retry storm with audit trail verification
#   - Verifying zero recovery commands across the full storm window
#   - Testing the exact transition boundary: last-in-cooldown → first-after-cooldown
#   - Counting audit events to verify 1:1 probe-to-event ratio
#   - Testing interleaved terminals under concurrent retry conditions

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

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$msg"
    else
        fail "$msg" "expected='$expected' actual='$actual'"
    fi
}

assert_file_contains() {
    local file="$1" pattern="$2" msg="$3"
    if grep -q "$pattern" "$file" 2>/dev/null; then
        pass "$msg"
    else
        fail "$msg" "pattern '$pattern' not found in $file"
    fi
}

assert_file_not_contains() {
    local file="$1" pattern="$2" msg="$3"
    if ! grep -q "$pattern" "$file" 2>/dev/null; then
        pass "$msg"
    else
        fail "$msg" "unexpected pattern '$pattern' found in $file"
    fi
}

count_lines() {
    local file="$1" pattern="$2"
    grep -c "$pattern" "$file" 2>/dev/null || echo "0"
}

# --- File-based mock infrastructure (same as PR-0 tests) ---
TMP_ROOT=$(mktemp -d)
MOCK_QUEUE="$TMP_ROOT/mock_queue"
MOCK_FAIL_FLAG="$TMP_ROOT/mock_fail_once"
MOCK_CALL_LOG="$TMP_ROOT/mock_calls"
STATE_DIR="$TMP_ROOT/state"
AUDIT_FILE="$STATE_DIR/blocked_dispatch_audit.ndjson"
mkdir -p "$STATE_DIR"
export STATE_DIR VNX_STATE_DIR="$STATE_DIR"

queue_responses() {
    printf '%s\n' "$@" > "$MOCK_QUEUE"
}

mock_fail_once() {
    touch "$MOCK_FAIL_FLAG"
}

reset_mocks() {
    rm -f "$MOCK_QUEUE" "$MOCK_FAIL_FLAG" "$MOCK_CALL_LOG" "$AUDIT_FILE"
    rm -rf "$STATE_DIR/input_mode_cooldown"
    touch "$MOCK_QUEUE" "$MOCK_CALL_LOG"
}

tmux() {
    local subcmd="$1"
    echo "$subcmd" >> "$MOCK_CALL_LOG"
    case "$subcmd" in
        display-message)
            if [ -f "$MOCK_FAIL_FLAG" ]; then
                rm -f "$MOCK_FAIL_FLAG"
                return 1
            fi
            if [ -s "$MOCK_QUEUE" ]; then
                local response
                response=$(head -1 "$MOCK_QUEUE")
                tail -n +2 "$MOCK_QUEUE" > "${MOCK_QUEUE}.tmp" && mv "${MOCK_QUEUE}.tmp" "$MOCK_QUEUE"
                printf '%s' "$response"
            else
                printf '0:0:'
            fi
            return 0
            ;;
        copy-mode|send-keys)
            return 0
            ;;
        *)
            return 0
            ;;
    esac
}

sleep()  { return 0; }
log()    { :; }

source "$PROJECT_ROOT/scripts/lib/input_mode_guard.sh"

echo "=== Scrollback Preservation Certification Tests ==="
echo ""

# ===========================================================================
# CERT-1: Retry storm simulation — 10 rapid retries with operator in copy-mode
#
# Scenario: Dispatcher retries every 2s, operator is scrolling through
# terminal output in copy-mode. After the first attempt (which recovers),
# the next 9 retries must NOT cancel copy-mode.
# ===========================================================================
echo "--- CERT-1: Retry storm (10 iterations) ---"
reset_mocks
_INPUT_MODE_COOLDOWN=30

# First attempt: pane blocked → full recovery → success
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "storm:0.1" "T2" "storm-001" || rc=$?
assert_pass $rc "CERT-1a: first attempt recovers successfully"

# Now simulate 9 more retry-loop iterations with operator in copy-mode
copy_mode_cancel_count=0
for i in $(seq 2 10); do
    rm -f "$MOCK_CALL_LOG"
    touch "$MOCK_CALL_LOG"
    queue_responses "1:0:copy-mode"

    rc=0; check_pane_input_ready "storm:0.1" "T2" "storm-$(printf '%03d' $i)" || rc=$?

    # Must be deferred (rc=1) during cooldown
    if [ "$rc" -eq 0 ]; then
        fail "CERT-1b: retry $i should be deferred" "returned 0 (allowed delivery)"
    fi

    # Check if any cancel command was sent
    if grep -q "^copy-mode$" "$MOCK_CALL_LOG" 2>/dev/null; then
        copy_mode_cancel_count=$((copy_mode_cancel_count + 1))
    fi
    if grep -q "^send-keys$" "$MOCK_CALL_LOG" 2>/dev/null; then
        copy_mode_cancel_count=$((copy_mode_cancel_count + 1))
    fi
done

assert_eq "0" "$copy_mode_cancel_count" \
    "CERT-1b: zero recovery commands sent across 9 retry-storm iterations"

# Verify audit trail has cooldown events for each deferred retry
# Each deferral emits 2 events: input_mode_recovery_cooldown + input_mode_delivery_blocked
# Count only the recovery_cooldown event type to get 1 per deferral.
cooldown_count=$(count_lines "$AUDIT_FILE" "input_mode_recovery_cooldown")
assert_eq "9" "$cooldown_count" \
    "CERT-1c: exactly 9 cooldown events (1 per retry iteration)"

# Verify only 1 recovery_started across entire storm
recovery_count=$(count_lines "$AUDIT_FILE" "input_mode_recovery_started")
assert_eq "1" "$recovery_count" \
    "CERT-1d: exactly 1 recovery attempt across entire 10-iteration storm"

echo ""

# ===========================================================================
# CERT-2: First attempt after cooldown gets full recovery
#
# Scenario: After the retry storm cooldown expires, the next blocked
# attempt must get full recovery (not be deferred).
# ===========================================================================
echo "--- CERT-2: Post-cooldown recovery ---"
reset_mocks
_INPUT_MODE_COOLDOWN=1  # 1s cooldown for test speed

# First attempt: triggers recovery + starts cooldown
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "cooldown:0.1" "T1" "cool-001" || true

# Immediate retry: deferred (in cooldown)
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "cooldown:0.1" "T1" "cool-002" || rc=$?
assert_fail $rc "CERT-2a: immediate retry deferred by cooldown"
assert_file_not_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-2a: no cancel during cooldown"

# Wait for cooldown to expire
builtin command sleep 1.5 2>/dev/null || /bin/sleep 1.5 2>/dev/null || sleep 1.5

# After cooldown: must get full recovery
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "cooldown:0.1" "T1" "cool-003" || rc=$?
assert_pass $rc "CERT-2b: post-cooldown attempt gets full recovery"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "CERT-2b: recovery_started event after cooldown expired"
assert_file_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-2b: copy-mode -q called (full recovery restored)"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "CERT-2b: no cooldown event after expiry"

_INPUT_MODE_COOLDOWN=30  # restore

echo ""

# ===========================================================================
# CERT-3: Dispatch-time safety — blocks delivery when recovery fails
#
# Scenario: First attempt on a fresh terminal, pane stuck in copy-mode,
# both recovery attempts fail. Must block delivery (fail-closed).
# ===========================================================================
echo "--- CERT-3: Dispatch-time safety (fail-closed) ---"
reset_mocks

# Both recovery attempts fail (pane stays in copy-mode)
queue_responses "1:0:copy-mode" "1:0:copy-mode" "1:0:copy-mode"
rc=0; check_pane_input_ready "safety:0.1" "T3" "safe-001" || rc=$?
assert_fail $rc "CERT-3a: delivery blocked when both recovery attempts fail"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_failed" \
    "CERT-3a: recovery_failed event emitted"
assert_file_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "CERT-3a: delivery_blocked event emitted"
assert_file_contains "$AUDIT_FILE" '"reason":"recovery_failed"' \
    "CERT-3a: reason=recovery_failed in blocked event"

# Verify recovery WAS attempted (not skipped — this is first attempt, no cooldown)
assert_file_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-3b: programmatic cancel attempted on first delivery"
assert_file_contains "$MOCK_CALL_LOG" "^send-keys$" \
    "CERT-3b: escape fallback attempted on first delivery"

# Both recovery attempts logged
recovery_attempts=$(count_lines "$AUDIT_FILE" "input_mode_recovery_started")
assert_eq "2" "$recovery_attempts" \
    "CERT-3c: exactly 2 recovery attempts on first delivery"

echo ""

# ===========================================================================
# CERT-4: Interleaved terminals — cooldown isolation
#
# Scenario: T1 and T2 both in retry loops. T1 in cooldown must not
# affect T2's recovery ability, and vice versa.
# ===========================================================================
echo "--- CERT-4: Per-terminal cooldown isolation ---"
reset_mocks

# T1: first attempt triggers recovery → starts cooldown
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "interleave:0.1" "T1" "inter-T1-001" || true

# T2: first attempt triggers recovery → starts cooldown
rm -f "$MOCK_CALL_LOG"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "interleave:0.2" "T2" "inter-T2-001" || true

# Verify T2 got full recovery (not blocked by T1's cooldown)
assert_file_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-4a: T2 gets full recovery despite T1 in cooldown"

# T1 retry: should be deferred (own cooldown active)
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "interleave:0.1" "T1" "inter-T1-002" || rc=$?
assert_fail $rc "CERT-4b: T1 retry deferred by own cooldown"
assert_file_not_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-4b: no cancel sent for T1 (in cooldown)"

# T2 retry: should also be deferred (own cooldown active)
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "interleave:0.2" "T2" "inter-T2-002" || rc=$?
assert_fail $rc "CERT-4c: T2 retry deferred by own cooldown"
assert_file_not_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "CERT-4c: no cancel sent for T2 (in cooldown)"

# Verify cooldown files exist for both terminals independently
assert_eq "true" "$([ -f "$STATE_DIR/input_mode_cooldown/T1" ] && echo true || echo false)" \
    "CERT-4d: T1 has independent cooldown file"
assert_eq "true" "$([ -f "$STATE_DIR/input_mode_cooldown/T2" ] && echo true || echo false)" \
    "CERT-4d: T2 has independent cooldown file"

echo ""

# ===========================================================================
# CERT-5: Normal-mode passthrough during storm — no unnecessary deferrals
#
# Scenario: Retry storm active (cooldown in effect), but operator exits
# copy-mode (pane returns to normal). Dispatch must proceed immediately.
# ===========================================================================
echo "--- CERT-5: Normal-mode passthrough during storm ---"
reset_mocks

# Start cooldown via first blocked attempt
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "passthru:0.1" "T2" "pass-001" || true

# Operator exits copy-mode → pane is normal
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "0:0:"
rc=0; check_pane_input_ready "passthru:0.1" "T2" "pass-002" || rc=$?
assert_pass $rc "CERT-5a: normal-mode pane proceeds during active cooldown"

# No recovery or cooldown events expected
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "CERT-5a: no cooldown deferral for normal-mode pane"
assert_file_not_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "CERT-5a: no recovery attempted for normal-mode pane"

echo ""

# ===========================================================================
# CERT-6: Audit evidence completeness — every probe produces an event
#
# Scenario: 5 rapid probes. Each must produce exactly 1 input_mode_probed event.
# ===========================================================================
echo "--- CERT-6: Audit evidence completeness ---"
reset_mocks
_INPUT_MODE_COOLDOWN=30

# First probe: normal mode
queue_responses "0:0:"
check_pane_input_ready "audit:0.1" "T3" "aud-001" || true

# Second probe: blocked → recovery → success (starts cooldown)
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "audit:0.1" "T3" "aud-002" || true

# Third, fourth, fifth: cooldown deferred
for i in 3 4 5; do
    queue_responses "1:0:copy-mode"
    check_pane_input_ready "audit:0.1" "T3" "aud-$(printf '%03d' $i)" || true
done

# Count probe events — must match number of calls
probe_count=$(count_lines "$AUDIT_FILE" "input_mode_probed")
assert_eq "5" "$probe_count" \
    "CERT-6a: exactly 5 input_mode_probed events for 5 calls"

# Count cooldown events — 3 (probes 3,4,5)
# Each deferral emits 2 events; count only input_mode_recovery_cooldown for 1-per-deferral.
cooldown_events=$(count_lines "$AUDIT_FILE" "input_mode_recovery_cooldown")
assert_eq "3" "$cooldown_events" \
    "CERT-6b: exactly 3 cooldown events for probes 3-5"

# Count recovery starts — 1 (only probe 2)
recovery_starts=$(count_lines "$AUDIT_FILE" "input_mode_recovery_started")
assert_eq "1" "$recovery_starts" \
    "CERT-6c: exactly 1 recovery attempt (probe 2 only)"

_INPUT_MODE_COOLDOWN=30  # restore

echo ""

# --- Cleanup ---
rm -rf "$TMP_ROOT"

# --- Summary ---
echo "=== Scrollback Preservation Certification Results ==="
echo "$PASS_COUNT passed, $FAIL_COUNT failed"
echo ""

[ "$FAIL_COUNT" -eq 0 ] || exit 1
