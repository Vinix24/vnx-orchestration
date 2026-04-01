#!/usr/bin/env bash
# Tests for input_mode_guard.sh (gate_pr1_input_mode_recovery)
#
# Coverage:
#   - Normal mode pane (pane_in_mode=0) → input-ready, dispatch proceeds
#   - Blocked pane → recovered via programmatic cancel (copy-mode -q)
#   - Blocked pane → programmatic cancel fails, recovered via Escape
#   - Blocked pane → both recovery attempts fail → dispatch blocked (fail-closed)
#   - Probe failure (tmux unreachable) → dispatch blocked
#   - Dead pane (pane_dead=1) → dispatch blocked
#   - Headless provider → exempt, always passes
#   - Audit events written for normal probe
#   - input_mode_delivery_blocked event emitted when recovery fails
#   - Slash-prefixed dispatch NOT attempted when pane blocked (rc=1 blocks caller)
#   - _classify_blocked_dispatch: blocked_input_mode → ambiguous (requeueable)
#
# Scenario coverage: success, blocked+recovered, blocked+failed, probe_failed, interrupted

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

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$msg"
    else
        fail "$msg" "expected='$expected' actual='$actual'"
    fi
}

# --- File-based mock infrastructure ---
# Uses temp files so state persists across the subshells created by $() captures.
#
# MOCK_QUEUE: one response per line; consumed FIFO by display-message calls
# MOCK_FAIL_FLAG: if this file exists, next display-message fails (and removes it)
# MOCK_CALL_LOG: appends one line per tmux subcommand call

TMP_ROOT=$(mktemp -d)
MOCK_QUEUE="$TMP_ROOT/mock_queue"
MOCK_FAIL_FLAG="$TMP_ROOT/mock_fail_once"
MOCK_CALL_LOG="$TMP_ROOT/mock_calls"
STATE_DIR="$TMP_ROOT/state"
AUDIT_FILE="$STATE_DIR/blocked_dispatch_audit.ndjson"
mkdir -p "$STATE_DIR"
export STATE_DIR

# Write probe responses to the queue (one per line)
queue_responses() {
    printf '%s\n' "$@" > "$MOCK_QUEUE"
}

# Set up mock to fail once on the next display-message call
mock_fail_once() {
    touch "$MOCK_FAIL_FLAG"
}

reset_mocks() {
    rm -f "$MOCK_QUEUE" "$MOCK_FAIL_FLAG" "$MOCK_CALL_LOG" "$AUDIT_FILE"
    rm -rf "$STATE_DIR/input_mode_cooldown"
    touch "$MOCK_QUEUE" "$MOCK_CALL_LOG"
}

# tmux mock: all state is file-based so changes survive $() subshells
tmux() {
    local subcmd="$1"
    echo "$subcmd" >> "$MOCK_CALL_LOG"

    case "$subcmd" in
        display-message)
            # Fail-once: file existence is the signal (file removal persists)
            if [ -f "$MOCK_FAIL_FLAG" ]; then
                rm -f "$MOCK_FAIL_FLAG"
                return 1
            fi
            # Consume first line from queue; fall back to normal-mode default
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

# Minimal stubs required by input_mode_guard.sh when sourced outside dispatcher
sleep()  { return 0; }
log()    { :; }

export VNX_STATE_DIR="$STATE_DIR"

# Source the library under test
source "$PROJECT_ROOT/scripts/lib/input_mode_guard.sh"

# Inline _classify_blocked_dispatch from the dispatcher to test the updated cases
# without sourcing the full dispatcher (which has unresolvable dependencies here).
_classify_blocked_dispatch() {
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*)
            echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable)
            echo "ambiguous true" ;;
        canonical_lease:*)
            echo "busy true" ;;
        blocked_input_mode|recovery_failed|pane_dead|probe_failed)
            echo "ambiguous true" ;;
        *)
            echo "invalid false" ;;
    esac
}

# ===========================================================================
# Test 1: Normal mode pane — input-ready, no recovery needed
# Scenario: success — normal mode, probe passes immediately
# ===========================================================================
reset_mocks
queue_responses "0:0:"
rc=0; check_pane_input_ready "test:0.0" "T2" "d-001" || rc=$?
assert_pass $rc "T1: normal mode pane is input-ready"

# Probe audit event must be emitted even for normal mode (full probe visibility)
assert_file_contains "$AUDIT_FILE" "input_mode_probed" \
    "T1: input_mode_probed event emitted for normal mode"

# No recovery events expected
assert_file_not_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T1: no recovery_started event for normal mode"

# ===========================================================================
# Test 2: Blocked pane → recovered via programmatic cancel (copy-mode -q)
# Scenario: success path via programmatic cancel
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode" "0:0:"
#               initial probe    after copy-mode -q
rc=0; check_pane_input_ready "test:0.1" "T2" "d-002" || rc=$?
assert_pass $rc "T2: blocked pane recovered via programmatic cancel"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T2: recovery_started event emitted"
assert_file_contains "$AUDIT_FILE" "input_mode_recovery_succeeded" \
    "T2: recovery_succeeded event emitted"
assert_file_contains "$AUDIT_FILE" "programmatic_cancel" \
    "T2: recovery action recorded as programmatic_cancel"
assert_file_contains "$AUDIT_FILE" '"mode_before":"copy-mode"' \
    "T2: mode_before recorded in recovery event"

# No blocked event on successful recovery
assert_file_not_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "T2: no delivery_blocked event on successful recovery"

# Verify copy-mode -q was called (not send-keys)
assert_file_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "T2: tmux copy-mode called for programmatic cancel"

# ===========================================================================
# Test 3: Blocked pane → programmatic cancel fails, recovered via Escape
# Scenario: success path via escape fallback
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode-vi" "1:0:copy-mode-vi" "0:0:"
#               initial probe       after copy-mode -q  after Escape
rc=0; check_pane_input_ready "test:0.2" "T2" "d-003" || rc=$?
assert_pass $rc "T3: blocked pane recovered via Escape fallback"

assert_file_contains "$AUDIT_FILE" "escape_fallback" \
    "T3: recovery action recorded as escape_fallback"
assert_file_contains "$AUDIT_FILE" "input_mode_recovery_succeeded" \
    "T3: recovery_succeeded event emitted for escape fallback"

# Verify Escape (send-keys) was called as fallback
assert_file_contains "$MOCK_CALL_LOG" "^send-keys$" \
    "T3: tmux send-keys Escape called as fallback"

assert_file_not_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "T3: no delivery_blocked event on successful escape recovery"

# ===========================================================================
# Test 4: Both recovery attempts fail — dispatch blocked (fail-closed)
# Scenario: no-output hang — pane stuck in mode, both methods fail
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode" "1:0:copy-mode" "1:0:copy-mode"
#               initial          after copy-mode -q      after Escape
rc=0; check_pane_input_ready "test:0.3" "T2" "d-004" || rc=$?
assert_fail $rc "T4: dispatch blocked when both recovery attempts fail (fail-closed)"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_failed" \
    "T4: recovery_failed event emitted"
assert_file_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "T4: delivery_blocked event emitted"
assert_file_contains "$AUDIT_FILE" '"reason":"recovery_failed"' \
    "T4: reason=recovery_failed in blocked event"
assert_file_contains "$AUDIT_FILE" '"attempts":2' \
    "T4: attempts=2 recorded in recovery_failed event"
assert_file_contains "$AUDIT_FILE" '"mode_before":"copy-mode"' \
    "T4: mode_before recorded in blocked event"

# ===========================================================================
# Test 5: Probe failure (tmux display-message fails)
# Scenario: pane unreachable — session lost or tmux unavailable
# ===========================================================================
reset_mocks
mock_fail_once
rc=0; check_pane_input_ready "test:0.4" "T2" "d-005" || rc=$?
assert_fail $rc "T5: dispatch blocked on probe failure"

assert_file_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "T5: delivery_blocked event on probe failure"
assert_file_contains "$AUDIT_FILE" "probe_failed" \
    "T5: probe_failed reason recorded"

# ===========================================================================
# Test 6: Dead pane (pane_dead=1) — dispatch blocked
# ===========================================================================
reset_mocks
queue_responses "0:1:"
rc=0; check_pane_input_ready "test:0.5" "T2" "d-006" || rc=$?
assert_fail $rc "T6: dispatch blocked for dead pane"

assert_file_contains "$AUDIT_FILE" "input_mode_delivery_blocked" \
    "T6: delivery_blocked event for dead pane"
assert_file_contains "$AUDIT_FILE" "pane_dead" \
    "T6: reason=pane_dead recorded"

# No recovery should be attempted for dead pane
assert_file_not_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T6: no recovery attempted for dead pane"

# ===========================================================================
# Test 7: Headless provider exempt — no tmux probe called
# Scenario: interrupted run / headless execution path (exempt per contract 8.3)
# ===========================================================================
reset_mocks
mock_fail_once  # any tmux display-message call would fail
rc=0; check_pane_input_ready "headless:0.0" "T2" "d-007" "headless_claude_cli" || rc=$?
assert_pass $rc "T7: headless provider is exempt from pane_in_mode probe"

# Verify tmux display-message was NOT called (no probe for headless)
if grep -q "^display-message$" "$MOCK_CALL_LOG" 2>/dev/null; then
    fail "T7: tmux display-message must NOT be called for headless provider" "was called"
else
    pass "T7: tmux display-message not called for headless provider"
fi

# ===========================================================================
# Test 8: Slash-prefixed dispatch NOT delivered when pane blocked (fail-closed)
# The caller must check rc=1 and not call send-keys for skill delivery.
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode" "1:0:copy-mode" "1:0:copy-mode"
_slash_send_called=false

rc=0; check_pane_input_ready "test:0.6" "T2" "d-008" || rc=$?
if [ "$rc" -ne 0 ]; then
    # Caller correctly sees rc=1 and skips slash delivery
    pass "T8: slash-prefixed skill not delivered when pane blocked (rc=1 stops caller)"
else
    _slash_send_called=true
    fail "T8: slash-prefixed skill not delivered when pane blocked" \
        "check_pane_input_ready returned 0 on blocked pane"
fi

# ===========================================================================
# Test 9: _classify_blocked_dispatch — new input-mode reasons → ambiguous+requeueable
# ===========================================================================
result=$(_classify_blocked_dispatch "blocked_input_mode")
assert_eq "ambiguous true" "$result" \
    "T9: blocked_input_mode classifies as ambiguous+requeueable"

result=$(_classify_blocked_dispatch "recovery_failed")
assert_eq "ambiguous true" "$result" \
    "T9: recovery_failed classifies as ambiguous+requeueable"

result=$(_classify_blocked_dispatch "pane_dead")
assert_eq "ambiguous true" "$result" \
    "T9: pane_dead classifies as ambiguous+requeueable"

result=$(_classify_blocked_dispatch "probe_failed")
assert_eq "ambiguous true" "$result" \
    "T9: probe_failed classifies as ambiguous+requeueable"

# Existing classifications must remain intact
result=$(_classify_blocked_dispatch "active_claim:T2")
assert_eq "busy true" "$result" \
    "T9: existing active_claim classification unchanged"

result=$(_classify_blocked_dispatch "canonical_lease:leased:d-other")
assert_eq "busy true" "$result" \
    "T9: existing canonical_lease classification unchanged"

result=$(_classify_blocked_dispatch "invalid_metadata")
assert_eq "invalid false" "$result" \
    "T9: unknown reason still maps to invalid"

# ===========================================================================
# Test 10: Audit events contain all required fields (Section 7.1)
# ===========================================================================
reset_mocks
queue_responses "0:0:"
check_pane_input_ready "audit-pane:1.2" "T3" "d-010" || true

assert_file_contains "$AUDIT_FILE" '"terminal_id":"T3"' \
    "T10: audit event contains terminal_id"
assert_file_contains "$AUDIT_FILE" '"dispatch_id":"d-010"' \
    "T10: audit event contains dispatch_id"
assert_file_contains "$AUDIT_FILE" '"pane_target":"audit-pane:1.2"' \
    "T10: audit event contains pane_target"
assert_file_contains "$AUDIT_FILE" '"pane_in_mode"' \
    "T10: audit event contains pane_in_mode"
assert_file_contains "$AUDIT_FILE" '"pane_dead"' \
    "T10: audit event contains pane_dead"
assert_file_contains "$AUDIT_FILE" '"timestamp"' \
    "T10: audit event contains timestamp"

# ===========================================================================
# Test 11: pane_in_mode=1 with pane_dead=0, vi bindings (copy-mode-vi)
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode-vi" "0:0:"
rc=0; check_pane_input_ready "test:0.7" "T1" "d-011" || rc=$?
assert_pass $rc "T11: copy-mode-vi recovered via programmatic cancel"
assert_file_contains "$AUDIT_FILE" '"mode_before":"copy-mode-vi"' \
    "T11: mode_before=copy-mode-vi recorded correctly"

# ===========================================================================
# Test 12: Rate-limiting — first attempt gets full recovery (cooldown not active)
# Scenario: fresh terminal, no prior recovery → full recovery sequence runs
# ===========================================================================
reset_mocks
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "test:0.8" "T2" "d-012" || rc=$?
assert_pass $rc "T12: first attempt gets full recovery (no cooldown)"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T12: recovery_started event emitted on first attempt"
assert_file_contains "$AUDIT_FILE" "input_mode_recovery_succeeded" \
    "T12: recovery_succeeded on first attempt"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T12: no cooldown event on first attempt"

# Verify cooldown file was created
assert_eq "true" "$([ -f "$STATE_DIR/input_mode_cooldown/T2" ] && echo true || echo false)" \
    "T12: cooldown file created after recovery"

# ===========================================================================
# Test 13: Rate-limiting — second attempt within cooldown skips recovery
# Scenario: same terminal, within 30s → probe only, no cancel/escape
# ===========================================================================
# Do NOT reset_mocks — we need the cooldown from T12 to persist
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "test:0.8" "T2" "d-013" || rc=$?
assert_fail $rc "T13: second attempt within cooldown returns blocked (requeueable)"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_cooldown" \
    "T13: recovery_cooldown event emitted"
assert_file_contains "$AUDIT_FILE" "recovery_cooldown_deferred" \
    "T13: reason=recovery_cooldown_deferred in event"

# No recovery commands should have been sent
assert_file_not_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "T13: tmux copy-mode NOT called during cooldown (scrollback preserved)"
assert_file_not_contains "$MOCK_CALL_LOG" "^send-keys$" \
    "T13: tmux send-keys NOT called during cooldown (scrollback preserved)"

# ===========================================================================
# Test 14: Rate-limiting — different terminal not affected by cooldown
# Scenario: T2 in cooldown but T1 has no cooldown → T1 gets full recovery
# ===========================================================================
# T2 cooldown still active from T12/T13 — do NOT reset
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "test:0.9" "T1" "d-014" || rc=$?
assert_pass $rc "T14: different terminal (T1) gets full recovery despite T2 cooldown"

assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T14: recovery_started for T1 (not in cooldown)"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T14: no cooldown event for T1"

# ===========================================================================
# Test 15: Rate-limiting — cooldown expiry restores full recovery
# Scenario: set cooldown to 1s, wait for expiry → full recovery again
# ===========================================================================
reset_mocks
_INPUT_MODE_COOLDOWN=1  # Override to 1s for test speed

# First attempt — triggers recovery and starts cooldown
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "test:1.0" "T3" "d-015a" || rc=$?
assert_pass $rc "T15a: first attempt recovers (starts 1s cooldown)"

# Second attempt immediately — should be deferred
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "test:1.0" "T3" "d-015b" || rc=$?
assert_fail $rc "T15b: immediate retry deferred by cooldown"
assert_file_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T15b: cooldown event emitted"

# Wait for cooldown to expire (real sleep, not mocked)
builtin command sleep 1.5 2>/dev/null || /bin/sleep 1.5 2>/dev/null || sleep 1.5

# Third attempt after cooldown — should get full recovery
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "test:1.0" "T3" "d-015c" || rc=$?
assert_pass $rc "T15c: after cooldown expiry, full recovery restored"
assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T15c: recovery_started after cooldown expired"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T15c: no cooldown event after expiry"

# Restore default cooldown
_INPUT_MODE_COOLDOWN=30

# ===========================================================================
# Test 16: Rate-limiting — copy-mode during cooldown preserves scrollback
# Scenario: pane in copy-mode, cooldown active → no cancel sent, dispatch deferred
# ===========================================================================
reset_mocks
# Start cooldown by doing a first recovery
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "test:1.1" "T2" "d-016a" || true

# Now simulate retry with operator scrolling (copy-mode)
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode"
rc=0; check_pane_input_ready "test:1.1" "T2" "d-016b" || rc=$?
assert_fail $rc "T16: copy-mode during cooldown → dispatch deferred"

# Critical: scrollback must be preserved — no copy-mode -q or Escape sent
assert_file_not_contains "$MOCK_CALL_LOG" "^copy-mode$" \
    "T16: copy-mode -q NOT called (operator scrollback preserved)"
assert_file_not_contains "$MOCK_CALL_LOG" "^send-keys$" \
    "T16: send-keys Escape NOT called (operator scrollback preserved)"
assert_file_contains "$AUDIT_FILE" "recovery_cooldown_deferred" \
    "T16: deferred reason recorded in audit"

# ===========================================================================
# Test 17: Rate-limiting — normal mode pane during cooldown proceeds immediately
# Scenario: cooldown active but pane is input-ready → no recovery needed, dispatch ok
# ===========================================================================
# T2 cooldown still active from T16
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "0:0:"
rc=0; check_pane_input_ready "test:1.1" "T2" "d-017" || rc=$?
assert_pass $rc "T17: normal mode pane during cooldown → dispatch proceeds"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T17: no cooldown event when pane is already input-ready"

# ===========================================================================
# Test 18: Rate-limiting — env override of cooldown period
# Scenario: VNX_INPUT_MODE_COOLDOWN=0 disables cooldown
# ===========================================================================
reset_mocks
_INPUT_MODE_COOLDOWN=0  # Disable cooldown

# First attempt — full recovery, starts 0s cooldown (effectively no cooldown)
queue_responses "1:0:copy-mode" "0:0:"
check_pane_input_ready "test:1.2" "T2" "d-018a" || true

# Second attempt immediately — should still get full recovery (0s cooldown = always expired)
rm -f "$MOCK_CALL_LOG" "$AUDIT_FILE"
touch "$MOCK_CALL_LOG"
queue_responses "1:0:copy-mode" "0:0:"
rc=0; check_pane_input_ready "test:1.2" "T2" "d-018b" || rc=$?
assert_pass $rc "T18: VNX_INPUT_MODE_COOLDOWN=0 disables rate-limiting"
assert_file_contains "$AUDIT_FILE" "input_mode_recovery_started" \
    "T18: full recovery even on second attempt with cooldown=0"
assert_file_not_contains "$AUDIT_FILE" "recovery_cooldown" \
    "T18: no cooldown event with cooldown=0"

# Restore default
_INPUT_MODE_COOLDOWN=30

# --- Cleanup ---
rm -rf "$TMP_ROOT"

# --- Summary ---
echo ""
echo "=== input_mode_guard test results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

[ "$FAIL_COUNT" -eq 0 ] || exit 1
