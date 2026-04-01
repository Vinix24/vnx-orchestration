#!/usr/bin/env bash
# Tests for RC-2 (canonical_check_parse_error classification) and
#         RC-5 (duplicate_delivery_prevented audit event reachability)
#
# Coverage:
#   RC-2 — _classify_blocked_dispatch() accuracy:
#     - canonical_check_parse_error   → ambiguous true  (was: invalid false — regression fix)
#     - canonical_lease_acquire_failed → ambiguous true  (was: invalid false — regression fix)
#     - input_mode_blocked            → ambiguous true  (was: invalid false — regression fix)
#     - Regression suite: all other block reasons mapped correctly
#   RC-5 — duplicate_delivery_prevented reachability:
#     - active_claim:<same_dispatch_id>                  → duplicate_delivery_prevented
#     - active_claim:<different_dispatch_id>             → dispatch_blocked
#     - canonical_lease:leased:<same_dispatch_id>        → duplicate_delivery_prevented
#     - canonical_lease:leased:<different_dispatch_id>   → dispatch_blocked
#     - Other block reasons                              → dispatch_blocked
#
# Scenario coverage: canonical_check_parse_error, canonical_lease_acquire_failed,
#                    input_mode_blocked, all busy/ambiguous/invalid regression cases,
#                    duplicate-vs-foreign active_claim, duplicate-vs-foreign canonical_lease

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [ "$actual" = "$expected" ]; then pass "$msg"
    else fail "$msg" "expected='$expected', actual='$actual'"; fi
}

# ---------------------------------------------------------------------------
# Mirror of _classify_blocked_dispatch() from dispatcher_v8_minimal.sh
# Must stay in sync. Changes to the dispatcher must be reflected here.
# ---------------------------------------------------------------------------
_classify_blocked_dispatch() {
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*)
            echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable)
            echo "ambiguous true" ;;
        canonical_check_parse_error|canonical_lease_acquire_failed)
            echo "ambiguous true" ;;
        canonical_lease:*)
            echo "busy true" ;;
        blocked_input_mode|recovery_failed|pane_dead|probe_failed|input_mode_blocked)
            echo "ambiguous true" ;;
        *)
            echo "invalid false" ;;
    esac
}

# ---------------------------------------------------------------------------
# Mirror of duplicate_delivery_prevented detection logic
# Extracted from two call sites in the dispatcher (lines ~315-323 and ~1532-1540)
# Returns the event_type that would be emitted for a given block_reason/dispatch_id pair.
# ---------------------------------------------------------------------------
_determine_event_type_legacy() {
    local block_reason="$1"
    local dispatch_id="$2"
    if [[ "$block_reason" == active_claim:* ]]; then
        local holder="${block_reason#active_claim:}"
        if [[ "$holder" == "$dispatch_id" ]]; then
            echo "duplicate_delivery_prevented"
        else
            echo "dispatch_blocked"
        fi
    else
        echo "dispatch_blocked"
    fi
}

_determine_event_type_canonical() {
    local block_reason="$1"
    local dispatch_id="$2"
    if [[ "$block_reason" == canonical_lease:leased:* ]]; then
        local current_holder="${block_reason#canonical_lease:leased:}"
        if [[ "$current_holder" == "$dispatch_id" ]]; then
            echo "duplicate_delivery_prevented"
        else
            echo "dispatch_blocked"
        fi
    else
        echo "dispatch_blocked"
    fi
}

# ===========================================================================
# RC-2 Tests: _classify_blocked_dispatch() accuracy
# ===========================================================================

# T1: canonical_check_parse_error → ambiguous true (was: invalid false before fix)
result=$(_classify_blocked_dispatch "canonical_check_parse_error")
assert_eq "$result" "ambiguous true" \
    "T1: canonical_check_parse_error classified as ambiguous true"

# T2: canonical_lease_acquire_failed → ambiguous true (was: invalid false before fix)
result=$(_classify_blocked_dispatch "canonical_lease_acquire_failed")
assert_eq "$result" "ambiguous true" \
    "T2: canonical_lease_acquire_failed classified as ambiguous true"

# T3: input_mode_blocked → ambiguous true (was: invalid false before fix)
result=$(_classify_blocked_dispatch "input_mode_blocked")
assert_eq "$result" "ambiguous true" \
    "T3: input_mode_blocked classified as ambiguous true"

# --- Regression suite: existing classifications must not change ---

# T4: active_claim → busy true
result=$(_classify_blocked_dispatch "active_claim:term-1")
assert_eq "$result" "busy true" "T4: active_claim classified as busy true"

# T5: status_claimed → busy true
result=$(_classify_blocked_dispatch "status_claimed:term-1")
assert_eq "$result" "busy true" "T5: status_claimed classified as busy true"

# T6: canonical_lease:leased → busy true
result=$(_classify_blocked_dispatch "canonical_lease:leased:dispatch-abc")
assert_eq "$result" "busy true" "T6: canonical_lease:leased classified as busy true"

# T7: canonical_lease:lease_expired → ambiguous true
result=$(_classify_blocked_dispatch "canonical_lease:lease_expired:dispatch-old")
assert_eq "$result" "ambiguous true" "T7: canonical_lease:lease_expired classified as ambiguous true"

# T8: canonical_lease:lease_expired_recovering → ambiguous true
result=$(_classify_blocked_dispatch "canonical_lease:lease_expired_recovering")
assert_eq "$result" "ambiguous true" \
    "T8: canonical_lease:lease_expired_recovering classified as ambiguous true"

# T9: recent_* → ambiguous true
result=$(_classify_blocked_dispatch "recent_activity:5s")
assert_eq "$result" "ambiguous true" "T9: recent_* classified as ambiguous true"

# T10: canonical_check_error → ambiguous true
result=$(_classify_blocked_dispatch "canonical_check_error:timeout")
assert_eq "$result" "ambiguous true" "T10: canonical_check_error classified as ambiguous true"

# T11: terminal_state_unreadable → ambiguous true
result=$(_classify_blocked_dispatch "terminal_state_unreadable")
assert_eq "$result" "ambiguous true" "T11: terminal_state_unreadable classified as ambiguous true"

# T12: blocked_input_mode → ambiguous true
result=$(_classify_blocked_dispatch "blocked_input_mode")
assert_eq "$result" "ambiguous true" "T12: blocked_input_mode classified as ambiguous true"

# T13: recovery_failed → ambiguous true
result=$(_classify_blocked_dispatch "recovery_failed")
assert_eq "$result" "ambiguous true" "T13: recovery_failed classified as ambiguous true"

# T14: pane_dead → ambiguous true
result=$(_classify_blocked_dispatch "pane_dead")
assert_eq "$result" "ambiguous true" "T14: pane_dead classified as ambiguous true"

# T15: probe_failed → ambiguous true
result=$(_classify_blocked_dispatch "probe_failed")
assert_eq "$result" "ambiguous true" "T15: probe_failed classified as ambiguous true"

# T16: unrecognized reason → invalid false (wildcard)
result=$(_classify_blocked_dispatch "unknown_reason_xyz")
assert_eq "$result" "invalid false" "T16: unrecognized reason classified as invalid false"

# T17: empty reason → invalid false (wildcard)
result=$(_classify_blocked_dispatch "")
assert_eq "$result" "invalid false" "T17: empty reason classified as invalid false"

# ===========================================================================
# RC-5 Tests: duplicate_delivery_prevented event type selection
# ===========================================================================

DISPATCH_ID="20260401-120000-test-dispatch-B"
OTHER_ID="20260401-110000-other-dispatch-A"

# --- Legacy lock path (active_claim) ---

# T18: active_claim held by SAME dispatch → duplicate_delivery_prevented
event=$(_determine_event_type_legacy "active_claim:${DISPATCH_ID}" "$DISPATCH_ID")
assert_eq "$event" "duplicate_delivery_prevented" \
    "T18: active_claim same-dispatch → duplicate_delivery_prevented"

# T19: active_claim held by DIFFERENT dispatch → dispatch_blocked
event=$(_determine_event_type_legacy "active_claim:${OTHER_ID}" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T19: active_claim different-dispatch → dispatch_blocked"

# T20: non-claim block reason via legacy path → dispatch_blocked
event=$(_determine_event_type_legacy "status_claimed:term-1" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T20: non-claim reason via legacy path → dispatch_blocked"

# --- Canonical lease path (canonical_lease:leased) ---

# T21: canonical_lease:leased by SAME dispatch → duplicate_delivery_prevented
event=$(_determine_event_type_canonical "canonical_lease:leased:${DISPATCH_ID}" "$DISPATCH_ID")
assert_eq "$event" "duplicate_delivery_prevented" \
    "T21: canonical_lease:leased same-dispatch → duplicate_delivery_prevented"

# T22: canonical_lease:leased by DIFFERENT dispatch → dispatch_blocked
event=$(_determine_event_type_canonical "canonical_lease:leased:${OTHER_ID}" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T22: canonical_lease:leased different-dispatch → dispatch_blocked"

# T23: canonical_lease:lease_expired via canonical path → dispatch_blocked
# (lease_expired is not a leased: prefix — falls to else branch)
event=$(_determine_event_type_canonical "canonical_lease:lease_expired:${DISPATCH_ID}" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T23: canonical_lease:lease_expired does NOT trigger duplicate_delivery_prevented"

# T24: non-lease reason via canonical path → dispatch_blocked
event=$(_determine_event_type_canonical "blocked_input_mode" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T24: non-lease reason via canonical path → dispatch_blocked"

# T25: dispatch_id prefix must match exactly — partial match is NOT a duplicate
PARTIAL_ID="${DISPATCH_ID%-B}"  # Strip suffix
event=$(_determine_event_type_canonical "canonical_lease:leased:${PARTIAL_ID}" "$DISPATCH_ID")
assert_eq "$event" "dispatch_blocked" \
    "T25: partial dispatch_id match does NOT trigger duplicate_delivery_prevented"

# T26: canonical_check_parse_error is requeueable (ambiguous) — classification test
# This verifies the fix is coherent: a parse error must never permanently reject.
result=$(_classify_blocked_dispatch "canonical_check_parse_error")
category="${result%% *}"
requeueable="${result##* }"
assert_eq "$category" "ambiguous" \
    "T26: canonical_check_parse_error category=ambiguous (not invalid)"
assert_eq "$requeueable" "true" \
    "T26: canonical_check_parse_error requeueable=true"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== test_classification_accuracy results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

[ "$FAIL_COUNT" -eq 0 ] || exit 1
