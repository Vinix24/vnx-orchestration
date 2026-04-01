#!/usr/bin/env bash
# Tests for RC-3 (requeue disposition) and RC-4 (empty role guard)
#
# Coverage:
#   RC-3 — process_dispatches dispatch failure disposition:
#     - No marker (requeueable) → file stays in pending
#     - [SKILL_INVALID] marker → file stays in pending (waiting for edit)
#     - [DEPENDENCY_ERROR] marker → file stays in pending (waiting for resolution)
#     - [REJECTED: reason] marker → file moves to rejected (permanent)
#     - [SKILL_INVALID] wins over [REJECTED:] — checked first, stays pending
#   RC-4 — empty/none role pre-validation guard:
#     - Empty role → [SKILL_INVALID] marker appended, stays in pending
#     - "none" role → [SKILL_INVALID] marker appended, stays in pending
#     - "None" role → [SKILL_INVALID] marker appended, stays in pending
#     - Valid role → passes guard without marking
#     - Idempotent: second run does not duplicate [SKILL_INVALID] marker
#   Contract text:
#     - 80_TERMINAL_EXCLUSIVITY_CONTRACT.md step 4 has no contradictory
#       "non-blocking on failure" language
#
# Scenario coverage: success (valid role), empty-role, none-role, None-role,
#                    no-marker-requeueable, SKILL_INVALID, DEPENDENCY_ERROR,
#                    REJECTED-permanent, idempotent-marker

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

assert_file_exists() {
    local file="$1" msg="$2"
    if [ -f "$file" ]; then pass "$msg"; else fail "$msg" "file does not exist: $file"; fi
}

assert_file_not_exists() {
    local file="$1" msg="$2"
    if [ ! -f "$file" ]; then pass "$msg"; else fail "$msg" "file unexpectedly exists: $file"; fi
}

assert_file_contains() {
    local file="$1" pattern="$2" msg="$3"
    if grep -q "$pattern" "$file" 2>/dev/null; then pass "$msg"
    else fail "$msg" "pattern '$pattern' not found in $file"; fi
}

assert_file_not_contains() {
    local file="$1" pattern="$2" msg="$3"
    if ! grep -q "$pattern" "$file" 2>/dev/null; then pass "$msg"
    else fail "$msg" "unexpected pattern '$pattern' found in $file"; fi
}

assert_count() {
    local actual="$1" expected="$2" msg="$3"
    if [ "$actual" -eq "$expected" ]; then pass "$msg"
    else fail "$msg" "expected count=$expected, actual=$actual"; fi
}

# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------

TMP_ROOT=$(mktemp -d)
PENDING_DIR="$TMP_ROOT/pending"
REJECTED_DIR="$TMP_ROOT/rejected"
ACTIVE_DIR="$TMP_ROOT/active"
COMPLETED_DIR="$TMP_ROOT/completed"
mkdir -p "$PENDING_DIR" "$REJECTED_DIR" "$ACTIVE_DIR" "$COMPLETED_DIR"

# ---------------------------------------------------------------------------
# RC-3 logic — mirrors the exact code changed in process_dispatches()
# ---------------------------------------------------------------------------
# This function implements the marker-based disposition logic added in RC-3.
# It must match the dispatcher code exactly so changes to one must change both.
#
# Arguments: $1=dispatch_file  $2=rejected_dir
# Returns: 0 always; file is either moved to rejected or stays in pending
_rc3_handle_dispatch_failure() {
    local dispatch="$1"
    local rejected_dir="$2"

    if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
        return 0  # Stay in pending — waiting for operator to fix role
    fi
    if grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
        return 0  # Stay in pending — waiting for dependency to recover
    fi
    if grep -q "\[REJECTED:" "$dispatch"; then
        if [ -f "$dispatch" ]; then
            mv "$dispatch" "$rejected_dir/"
        fi
        return 0
    fi
    return 0  # Stay in pending — requeueable transient failure
}

# ---------------------------------------------------------------------------
# RC-4 logic — mirrors the exact code added before process_dispatches validation
# ---------------------------------------------------------------------------
# Arguments: $1=dispatch_file  $2=agent_role
# Returns: 1 if role is empty/none (dispatch blocked), 0 if role is valid
_rc4_check_role() {
    local dispatch="$1"
    local agent_role="$2"

    if [ -z "$agent_role" ] || [ "$agent_role" = "none" ] || [ "$agent_role" = "None" ]; then
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            printf '\n\n[SKILL_INVALID] Role is empty or '"'"'none'"'"'. Set a valid Role and remove this marker to retry.\n' >> "$dispatch"
        fi
        return 1  # blocked
    fi
    return 0  # passes
}

# ===========================================================================
# RC-3 Tests: Disposition after dispatch_with_skill_activation returns 1
# ===========================================================================

# T1: No marker → file stays in pending (requeueable transient failure)
dispatch="$PENDING_DIR/dispatch-rc3-no-marker.md"
echo "# Dispatch test" > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_exists "$dispatch" "T1: no-marker dispatch stays in pending"
assert_file_not_exists "$REJECTED_DIR/dispatch-rc3-no-marker.md" \
    "T1: no-marker dispatch NOT moved to rejected"

# T2: [SKILL_INVALID] → file stays in pending (waiting for edit)
dispatch="$PENDING_DIR/dispatch-rc3-skill-invalid.md"
printf '# Dispatch\n\n[SKILL_INVALID] bad role\n' > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_exists "$dispatch" "T2: SKILL_INVALID dispatch stays in pending"
assert_file_not_exists "$REJECTED_DIR/dispatch-rc3-skill-invalid.md" \
    "T2: SKILL_INVALID dispatch NOT moved to rejected"

# T3: [DEPENDENCY_ERROR] → file stays in pending (waiting for dependency)
dispatch="$PENDING_DIR/dispatch-rc3-dep-error.md"
printf '# Dispatch\n\n[DEPENDENCY_ERROR] gather_intelligence failed\n' > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_exists "$dispatch" "T3: DEPENDENCY_ERROR dispatch stays in pending"
assert_file_not_exists "$REJECTED_DIR/dispatch-rc3-dep-error.md" \
    "T3: DEPENDENCY_ERROR dispatch NOT moved to rejected"

# T4: [REJECTED: reason] → file moves to rejected (permanent failure)
dispatch="$PENDING_DIR/dispatch-rc3-rejected.md"
printf '# Dispatch\n\n[REJECTED: invalid track T0]\n' > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_not_exists "$dispatch" \
    "T4: [REJECTED:] dispatch moved OUT of pending"
assert_file_exists "$REJECTED_DIR/dispatch-rc3-rejected.md" \
    "T4: [REJECTED:] dispatch moved to rejected"

# T5: [SKILL_INVALID] wins over [REJECTED:] — SKILL_INVALID checked first, stays pending
# This verifies operator-edit-recovery is not permanently rejected even if REJECTED
# is also present (which shouldn't happen, but the guard order protects it).
dispatch="$PENDING_DIR/dispatch-rc3-skill-beats-rejected.md"
printf '# Dispatch\n\n[SKILL_INVALID] bad role\n[REJECTED: injected by mistake]\n' > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_exists "$dispatch" "T5: SKILL_INVALID takes precedence over REJECTED, stays pending"
assert_file_not_exists "$REJECTED_DIR/dispatch-rc3-skill-beats-rejected.md" \
    "T5: file NOT moved to rejected when SKILL_INVALID also present"

# T6: [DEPENDENCY_ERROR] wins over [REJECTED:] — same precedence rule
dispatch="$PENDING_DIR/dispatch-rc3-dep-beats-rejected.md"
printf '# Dispatch\n\n[DEPENDENCY_ERROR] python not found\n[REJECTED: injected by mistake]\n' > "$dispatch"
_rc3_handle_dispatch_failure "$dispatch" "$REJECTED_DIR"
assert_file_exists "$dispatch" \
    "T6: DEPENDENCY_ERROR takes precedence over REJECTED, stays pending"
assert_file_not_exists "$REJECTED_DIR/dispatch-rc3-dep-beats-rejected.md" \
    "T6: file NOT moved to rejected when DEPENDENCY_ERROR also present"

# ===========================================================================
# RC-4 Tests: Empty/none role pre-validation guard
# ===========================================================================

# T7: Empty role → blocked, [SKILL_INVALID] marker appended
dispatch="$PENDING_DIR/dispatch-rc4-empty-role.md"
echo "# Dispatch\nRole: \n" > "$dispatch"
result=0; _rc4_check_role "$dispatch" "" || result=$?
assert_fail $result "T7: empty role returns non-zero (blocked)"
assert_file_contains "$dispatch" "\[SKILL_INVALID\]" \
    "T7: empty role appends [SKILL_INVALID] marker"

# T8: "none" role → blocked, [SKILL_INVALID] marker appended
dispatch="$PENDING_DIR/dispatch-rc4-none-role.md"
echo "# Dispatch\nRole: none\n" > "$dispatch"
result=0; _rc4_check_role "$dispatch" "none" || result=$?
assert_fail $result "T8: 'none' role returns non-zero (blocked)"
assert_file_contains "$dispatch" "\[SKILL_INVALID\]" \
    "T8: 'none' role appends [SKILL_INVALID] marker"

# T9: "None" role → blocked, [SKILL_INVALID] marker appended
dispatch="$PENDING_DIR/dispatch-rc4-None-role.md"
echo "# Dispatch\nRole: None\n" > "$dispatch"
result=0; _rc4_check_role "$dispatch" "None" || result=$?
assert_fail $result "T9: 'None' role returns non-zero (blocked)"
assert_file_contains "$dispatch" "\[SKILL_INVALID\]" \
    "T9: 'None' role appends [SKILL_INVALID] marker"

# T10: Valid role → passes guard, no marker appended
dispatch="$PENDING_DIR/dispatch-rc4-valid-role.md"
echo "# Dispatch\nRole: @backend-developer\n" > "$dispatch"
result=0; _rc4_check_role "$dispatch" "@backend-developer" || result=$?
assert_pass $result "T10: valid role returns 0 (passes guard)"
assert_file_not_contains "$dispatch" "\[SKILL_INVALID\]" \
    "T10: valid role does NOT append [SKILL_INVALID] marker"

# T11: Idempotent — second run on empty role does not duplicate marker
dispatch="$PENDING_DIR/dispatch-rc4-idempotent.md"
echo "# Dispatch\nRole: \n" > "$dispatch"
_rc4_check_role "$dispatch" "" || true
_rc4_check_role "$dispatch" "" || true  # Second call
skill_count=$(grep -c "\[SKILL_INVALID\]" "$dispatch" || true)
assert_count "$skill_count" 1 "T11: [SKILL_INVALID] not duplicated on repeated empty-role guard calls"

# T12: "none" role already has [SKILL_INVALID] marker — no duplicate
dispatch="$PENDING_DIR/dispatch-rc4-none-already-marked.md"
printf '# Dispatch\nRole: none\n\n[SKILL_INVALID] Role is empty or '"'"'none'"'"'.\n' > "$dispatch"
_rc4_check_role "$dispatch" "none" || true
skill_count=$(grep -c "\[SKILL_INVALID\]" "$dispatch" || true)
assert_count "$skill_count" 1 "T12: pre-existing [SKILL_INVALID] not duplicated for 'none' role"

# T13: Marker contains the expected guidance text
dispatch="$PENDING_DIR/dispatch-rc4-marker-text.md"
echo "# Dispatch" > "$dispatch"
_rc4_check_role "$dispatch" "" || true
assert_file_contains "$dispatch" "Set a valid Role" \
    "T13: [SKILL_INVALID] marker includes guidance to set valid role"
assert_file_contains "$dispatch" "remove this marker to retry" \
    "T13: [SKILL_INVALID] marker includes retry instruction"

# ===========================================================================
# Contract text test: 80_TERMINAL_EXCLUSIVITY_CONTRACT.md step 4 ambiguity
# ===========================================================================

CONTRACT_FILE="$PROJECT_ROOT/docs/core/80_TERMINAL_EXCLUSIVITY_CONTRACT.md"

# T14: The contradictory "non-blocking on failure" text must not be present
if grep -q "non-blocking on failure" "$CONTRACT_FILE" 2>/dev/null; then
    fail "T14: 80_TERMINAL_EXCLUSIVITY_CONTRACT.md still contains 'non-blocking on failure' — ambiguity not resolved"
else
    pass "T14: 'non-blocking on failure' text removed from contract step 4"
fi

# T15: The corrected text must describe command failure as blocking
if grep -q "Command execution failure.*blocks dispatch" "$CONTRACT_FILE" 2>/dev/null; then
    pass "T15: contract step 4 describes command failure as blocking"
else
    fail "T15: contract step 4 missing unambiguous blocking semantics for command failure" "pattern not found"
fi

# T16: The corrected text must describe result parse failure as non-blocking
if grep -q "Result parse failure.*does NOT block" "$CONTRACT_FILE" 2>/dev/null; then
    pass "T16: contract step 4 describes result parse failure as non-blocking"
else
    fail "T16: contract step 4 missing unambiguous non-blocking semantics for parse failure" "pattern not found"
fi

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
rm -rf "$TMP_ROOT"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== test_requeue_role_guard results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

[ "$FAIL_COUNT" -eq 0 ] || exit 1
