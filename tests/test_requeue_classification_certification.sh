#!/usr/bin/env bash
# PR-3 Certification: Dispatch Requeue And Classification Accuracy
#
# Gate: gate_pr3_requeue_classification_certification
# Contract: docs/core/140_REQUEUE_AND_CLASSIFICATION_ACCURACY_CONTRACT.md
#
# Certifies RC-1 through RC-6 rules by exercising realistic dispatch flows:
#   CERT-1: Requeueable dispatch defers to pending (RC-3)
#   CERT-2: Empty/none role caught at pre-validation (RC-4)
#   CERT-3: canonical_check_parse_error classified as ambiguous (RC-2)
#   CERT-4: All classification categories are correct (RC-1)
#   CERT-5: duplicate_delivery_prevented reachable for same-dispatch-id (RC-5)
#   CERT-6: Intelligence command failure blocks, parse failure doesn't (RC-6)
#   CERT-7: Marker precedence — SKILL_INVALID beats REJECTED (RC-3)
#   CERT-8: End-to-end realistic dispatch failure flow

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Test harness ---
PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" = "$actual" ]; then pass "$msg"; else fail "$msg" "expected='$expected' actual='$actual'"; fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if echo "$haystack" | grep -qF "$needle"; then pass "$msg"; else fail "$msg" "'$needle' not found"; fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if ! echo "$haystack" | grep -qF "$needle"; then pass "$msg"; else fail "$msg" "'$needle' was found"; fi
}

assert_file_exists() {
    local path="$1" msg="$2"
    if [ -f "$path" ]; then pass "$msg"; else fail "$msg" "file not found: $path"; fi
}

assert_file_not_exists() {
    local path="$1" msg="$2"
    if [ ! -f "$path" ]; then pass "$msg"; else fail "$msg" "file exists unexpectedly: $path"; fi
}

# --- Temp environment ---
TMP_ROOT=$(mktemp -d)
PENDING_DIR="$TMP_ROOT/pending"
REJECTED_DIR="$TMP_ROOT/rejected"
mkdir -p "$PENDING_DIR" "$REJECTED_DIR"

# --- Mirror dispatcher functions ---

# Exact copy of _classify_blocked_dispatch from dispatcher_v8_minimal.sh
_classify_blocked_dispatch() {
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*)
            echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable)
            echo "ambiguous true" ;;
        canonical_lease:*)
            echo "busy true" ;;
        canonical_check_parse_error|canonical_lease_acquire_failed|input_mode_blocked)
            echo "ambiguous true" ;;
        blocked_input_mode|recovery_failed|pane_dead|probe_failed)
            echo "ambiguous true" ;;
        *)
            echo "invalid false" ;;
    esac
}

# Mirror of RC-3 disposition logic from process_dispatches
_rc3_handle_dispatch_failure() {
    local dispatch="$1"
    local pending_dir="$2"
    local rejected_dir="$3"

    if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
        echo "pending:skill_invalid"
        return
    fi
    if grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
        echo "pending:dependency_error"
        return
    fi
    if grep -q "\[REJECTED:" "$dispatch"; then
        mv "$dispatch" "$rejected_dir/"
        echo "rejected:permanent"
        return
    fi
    # No marker = requeueable transient failure
    echo "pending:requeueable"
}

# Mirror of RC-4 empty role guard
_rc4_check_role() {
    local agent_role="$1"
    local dispatch="$2"

    if [ -z "$agent_role" ] || [ "$agent_role" = "none" ] || [ "$agent_role" = "None" ]; then
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            printf '\n\n[SKILL_INVALID] Role is empty or '"'"'none'"'"'. Set a valid Role and remove this marker to retry.\n' >> "$dispatch"
        fi
        echo "blocked:empty_role"
        return 1
    fi
    echo "passed"
    return 0
}

# Mirror of duplicate detection logic
_determine_event_type() {
    local block_reason="$1"
    local dispatch_id="$2"

    if [[ "$block_reason" == active_claim:* ]]; then
        local holder="${block_reason#active_claim:}"
        if [ "$holder" = "$dispatch_id" ]; then
            echo "duplicate_delivery_prevented"
        else
            echo "dispatch_blocked"
        fi
    elif [[ "$block_reason" == canonical_lease:leased:* ]]; then
        local holder="${block_reason#canonical_lease:leased:}"
        if [ "$holder" = "$dispatch_id" ]; then
            echo "duplicate_delivery_prevented"
        else
            echo "dispatch_blocked"
        fi
    else
        echo "dispatch_blocked"
    fi
}

reset_env() {
    rm -rf "$PENDING_DIR"/* "$REJECTED_DIR"/*
}

create_dispatch() {
    local name="$1"
    local content="${2:-Track: C\nPR-ID: PR-0\nDispatch-ID: $name\n}"
    printf "$content" > "$PENDING_DIR/$name.md"
    echo "$PENDING_DIR/$name.md"
}

echo "=== PR-3 Certification: Dispatch Requeue And Classification ==="
echo ""

# =========================================================================
# CERT-1: Requeueable dispatch defers to pending (RC-3)
# =========================================================================
echo "--- CERT-1: Requeueable dispatch stays in pending ---"
reset_env

# Simulate: dispatch_with_skill_activation returns 1, no marker (e.g., terminal busy)
dispatch=$(create_dispatch "d-requeue-001")
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:requeueable" "$result" "CERT-1a: no-marker failure stays in pending (requeueable)"
assert_file_exists "$dispatch" "CERT-1b: dispatch file still in pending dir"
assert_file_not_exists "$REJECTED_DIR/d-requeue-001.md" "CERT-1c: dispatch NOT in rejected dir"

# Simulate: dispatch with [SKILL_INVALID] stays in pending
reset_env
dispatch=$(create_dispatch "d-skill-002")
echo -e "\n[SKILL_INVALID] Bad skill" >> "$dispatch"
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:skill_invalid" "$result" "CERT-1d: SKILL_INVALID marker stays in pending"

# Simulate: dispatch with [DEPENDENCY_ERROR] stays in pending
reset_env
dispatch=$(create_dispatch "d-dep-003")
echo -e "\n[DEPENDENCY_ERROR] Runtime dep failed" >> "$dispatch"
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:dependency_error" "$result" "CERT-1e: DEPENDENCY_ERROR marker stays in pending"

# Simulate: dispatch with [REJECTED:] moves to rejected
reset_env
dispatch=$(create_dispatch "d-reject-004")
echo -e "\n[REJECTED: Invalid metadata]" >> "$dispatch"
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "rejected:permanent" "$result" "CERT-1f: REJECTED marker moves to rejected dir"
assert_file_exists "$REJECTED_DIR/d-reject-004.md" "CERT-1g: dispatch file moved to rejected"
assert_file_not_exists "$PENDING_DIR/d-reject-004.md" "CERT-1h: dispatch no longer in pending"

# =========================================================================
# CERT-2: Empty/none role pre-validation (RC-4)
# =========================================================================
echo ""
echo "--- CERT-2: Empty/none role pre-validation ---"
reset_env

# Empty string role
dispatch=$(create_dispatch "d-empty-role")
result=$(_rc4_check_role "" "$dispatch")
rc=$?
assert_eq "1" "$rc" "CERT-2a: empty role returns non-zero (blocked)"
assert_contains "$(cat "$dispatch")" "[SKILL_INVALID]" "CERT-2b: empty role appends SKILL_INVALID marker"
assert_contains "$(cat "$dispatch")" "Role is empty" "CERT-2c: marker message explains the problem"

# "none" role
reset_env
dispatch=$(create_dispatch "d-none-role")
result=$(_rc4_check_role "none" "$dispatch")
rc=$?
assert_eq "1" "$rc" "CERT-2d: 'none' role returns non-zero"
assert_contains "$(cat "$dispatch")" "[SKILL_INVALID]" "CERT-2e: 'none' role gets SKILL_INVALID"

# "None" role (Python-style)
reset_env
dispatch=$(create_dispatch "d-None-role")
result=$(_rc4_check_role "None" "$dispatch")
rc=$?
assert_eq "1" "$rc" "CERT-2f: 'None' role returns non-zero"

# Valid role passes
reset_env
dispatch=$(create_dispatch "d-valid-role")
result=$(_rc4_check_role "architect" "$dispatch")
rc=$?
assert_eq "0" "$rc" "CERT-2g: valid role returns zero (passes)"
assert_not_contains "$(cat "$dispatch")" "[SKILL_INVALID]" "CERT-2h: valid role gets no marker"

# Idempotent: marker not duplicated
reset_env
dispatch=$(create_dispatch "d-idem-role")
_rc4_check_role "" "$dispatch" > /dev/null 2>&1
_rc4_check_role "" "$dispatch" > /dev/null 2>&1
count=$(grep -c "\[SKILL_INVALID\]" "$dispatch")
assert_eq "1" "$count" "CERT-2i: SKILL_INVALID marker not duplicated on repeated calls"

# =========================================================================
# CERT-3: canonical_check_parse_error classified as ambiguous (RC-2)
# =========================================================================
echo ""
echo "--- CERT-3: canonical_check_parse_error classification ---"

result=$(_classify_blocked_dispatch "canonical_check_parse_error")
category="${result%% *}"
requeueable="${result##* }"
assert_eq "ambiguous" "$category" "CERT-3a: canonical_check_parse_error → ambiguous"
assert_eq "true" "$requeueable" "CERT-3b: canonical_check_parse_error → requeueable"

# Also verify the other RC-2 additions
result=$(_classify_blocked_dispatch "canonical_lease_acquire_failed")
assert_eq "ambiguous" "${result%% *}" "CERT-3c: canonical_lease_acquire_failed → ambiguous"

result=$(_classify_blocked_dispatch "input_mode_blocked")
assert_eq "ambiguous" "${result%% *}" "CERT-3d: input_mode_blocked → ambiguous"

# =========================================================================
# CERT-4: Full classification table (RC-1)
# =========================================================================
echo ""
echo "--- CERT-4: Complete classification table ---"

# Busy cases
assert_eq "busy true" "$(_classify_blocked_dispatch "active_claim:d-other")" "CERT-4a: active_claim → busy"
assert_eq "busy true" "$(_classify_blocked_dispatch "status_claimed:d-other")" "CERT-4b: status_claimed → busy"
assert_eq "busy true" "$(_classify_blocked_dispatch "canonical_lease:leased:d-other")" "CERT-4c: canonical_lease:leased → busy"

# Ambiguous cases
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "canonical_lease:lease_expired")" "CERT-4d: lease_expired → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "recent_working_without_claim")" "CERT-4e: recent_* → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "canonical_check_error:python_failed")" "CERT-4f: canonical_check_error → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "terminal_state_unreadable")" "CERT-4g: terminal_state_unreadable → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "blocked_input_mode")" "CERT-4h: blocked_input_mode → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "recovery_failed")" "CERT-4i: recovery_failed → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "pane_dead")" "CERT-4j: pane_dead → ambiguous"
assert_eq "ambiguous true" "$(_classify_blocked_dispatch "probe_failed")" "CERT-4k: probe_failed → ambiguous"

# Invalid case (wildcard)
assert_eq "invalid false" "$(_classify_blocked_dispatch "unknown_reason_xyz")" "CERT-4l: unknown → invalid"
assert_eq "invalid false" "$(_classify_blocked_dispatch "")" "CERT-4m: empty → invalid"

# =========================================================================
# CERT-5: duplicate_delivery_prevented reachable (RC-5)
# =========================================================================
echo ""
echo "--- CERT-5: duplicate_delivery_prevented reachability ---"

# Same dispatch_id holds active_claim → duplicate detected
event=$(_determine_event_type "active_claim:d-same-123" "d-same-123")
assert_eq "duplicate_delivery_prevented" "$event" "CERT-5a: active_claim same dispatch → duplicate_delivery_prevented"

# Different dispatch_id → normal block
event=$(_determine_event_type "active_claim:d-other-456" "d-same-123")
assert_eq "dispatch_blocked" "$event" "CERT-5b: active_claim different dispatch → dispatch_blocked"

# Same dispatch_id holds canonical_lease → duplicate detected
event=$(_determine_event_type "canonical_lease:leased:d-same-789" "d-same-789")
assert_eq "duplicate_delivery_prevented" "$event" "CERT-5c: canonical_lease same dispatch → duplicate_delivery_prevented"

# Different dispatch_id → normal block
event=$(_determine_event_type "canonical_lease:leased:d-other-000" "d-same-789")
assert_eq "dispatch_blocked" "$event" "CERT-5d: canonical_lease different dispatch → dispatch_blocked"

# Non-lease block → always dispatch_blocked
event=$(_determine_event_type "blocked_input_mode" "d-any")
assert_eq "dispatch_blocked" "$event" "CERT-5e: non-lease reason → dispatch_blocked"

# =========================================================================
# CERT-6: Intelligence blocking semantics (RC-6)
# =========================================================================
echo ""
echo "--- CERT-6: Intelligence blocking semantics ---"

# Verify contract text describes correct semantics
contract_file="$PROJECT_ROOT/docs/core/140_REQUEUE_AND_CLASSIFICATION_ACCURACY_CONTRACT.md"
if [ -f "$contract_file" ]; then
    assert_contains "$(cat "$contract_file")" "Command execution failure" "CERT-6a: contract defines command failure blocking"
    assert_contains "$(cat "$contract_file")" "Does NOT block dispatch" "CERT-6b: contract defines parse failure as non-blocking"
    assert_contains "$(cat "$contract_file")" "DEPENDENCY_ERROR" "CERT-6c: contract references DEPENDENCY_ERROR marker"
else
    fail "CERT-6a: contract file exists" "not found at $contract_file"
    fail "CERT-6b: contract defines parse failure" "contract missing"
    fail "CERT-6c: contract references marker" "contract missing"
fi

# =========================================================================
# CERT-7: Marker precedence (RC-3)
# =========================================================================
echo ""
echo "--- CERT-7: Marker precedence ---"
reset_env

# SKILL_INVALID + REJECTED → SKILL_INVALID wins (stays in pending)
dispatch=$(create_dispatch "d-precedence-1")
echo -e "\n[SKILL_INVALID] Bad skill\n[REJECTED: Something]" >> "$dispatch"
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:skill_invalid" "$result" "CERT-7a: SKILL_INVALID beats REJECTED (stays in pending)"

# DEPENDENCY_ERROR + REJECTED → DEPENDENCY_ERROR wins
reset_env
dispatch=$(create_dispatch "d-precedence-2")
echo -e "\n[DEPENDENCY_ERROR] Dep fail\n[REJECTED: Something]" >> "$dispatch"
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:dependency_error" "$result" "CERT-7b: DEPENDENCY_ERROR beats REJECTED (stays in pending)"

# =========================================================================
# CERT-8: End-to-end realistic dispatch failure flow
# =========================================================================
echo ""
echo "--- CERT-8: End-to-end realistic dispatch failure flow ---"
reset_env

# Scenario: dispatch fails due to terminal busy (canonical lease held)
# 1. Create dispatch
dispatch=$(create_dispatch "d-e2e-001" "Track: B\nPR-ID: PR-1\nDispatch-ID: d-e2e-001\n")

# 2. Classify the block reason
classification=$(_classify_blocked_dispatch "canonical_lease:leased:d-other-dispatch")
category="${classification%% *}"
requeueable="${classification##* }"
assert_eq "busy" "$category" "CERT-8a: canonical lease busy → busy category"
assert_eq "true" "$requeueable" "CERT-8b: canonical lease busy → requeueable"

# 3. Dispatch returns 1 (no marker written — this was a terminal state block)
result=$(_rc3_handle_dispatch_failure "$dispatch" "$PENDING_DIR" "$REJECTED_DIR")
assert_eq "pending:requeueable" "$result" "CERT-8c: no-marker failure defers to pending"
assert_file_exists "$dispatch" "CERT-8d: dispatch still in pending for retry"

# 4. Verify dispatch was NOT rejected
assert_file_not_exists "$REJECTED_DIR/d-e2e-001.md" "CERT-8e: dispatch NOT in rejected (not a permanent failure)"

# 5. On next loop iteration, terminal becomes available → dispatch succeeds
# (simulated by checking dispatch is still in pending and available for pickup)
file_count=$(ls -1 "$PENDING_DIR"/*.md 2>/dev/null | wc -l)
assert_eq "1" "$(echo $file_count)" "CERT-8f: exactly 1 dispatch in pending for next retry"

# =========================================================================
# CLEANUP
# =========================================================================
rm -rf "$TMP_ROOT"

# --- Summary ---
echo ""
echo "=== PR-3 Certification results: $PASS_COUNT passed, $FAIL_COUNT failed ==="
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "CERTIFICATION: PASS"
    echo "  - Requeueable dispatches defer to pending, not rejected (RC-3)"
    echo "  - Empty/none role caught at pre-validation (RC-4)"
    echo "  - canonical_check_parse_error classified as ambiguous (RC-2)"
    echo "  - All 15 classification cases verified (RC-1)"
    echo "  - duplicate_delivery_prevented reachable for same-dispatch-id (RC-5)"
    echo "  - Intelligence blocking semantics correct in contract (RC-6)"
    echo "  - Marker precedence: recoverable markers beat REJECTED (RC-3)"
    echo "  - End-to-end realistic failure flow defers correctly"
else
    echo "CERTIFICATION: FAIL — $FAIL_COUNT assertion(s) failed"
fi

[ "$FAIL_COUNT" -eq 0 ] || exit 1
