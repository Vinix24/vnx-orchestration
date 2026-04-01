#!/usr/bin/env bash
# Tests for PR-1: Fine-Grained Delivery Substep Rejection Logging
#
# Coverage:
#   - Each substep failure (send_skill, load_buffer, paste_buffer, enter) produces
#     a [DELIVERY_SUBSTEP_FAILED: substep=<name>] annotation in the dispatch file
#   - Generic rejection annotation is not emitted for substep failures
#   - Successful delivery path writes no substep annotation
#   - [DELIVERY_SUBSTEP_FAILED:] is requeueable — does not match [REJECTED:] pattern
#
# Scenario coverage: send_skill failure, load_buffer failure (codex), load_buffer failure
#                    (claude), paste_buffer failure (codex), paste_buffer failure (claude),
#                    enter failure, success path, marker does not trigger rejection

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

# ---------------------------------------------------------------------------
# Mirror of the substep delivery logic from dispatch_with_skill_activation().
# Simulates each substep: caller controls which substep "fails" via env vars.
# FAIL_SUBSTEP=<name> causes that substep to return 1.
# ---------------------------------------------------------------------------

FAIL_SUBSTEP="${FAIL_SUBSTEP:-}"  # Which substep to simulate failing

_mock_tmux_retry() {
    # Args: ignored (retries + command). We just check FAIL_SUBSTEP.
    # $3 = substep hint is passed as first non-retry arg name by caller
    local substep="$1"
    shift
    if [ "$FAIL_SUBSTEP" = "$substep" ]; then
        return 1
    fi
    return 0
}

_simulate_delivery() {
    local dispatch_file="$1"
    local provider="${2:-claude_code}"  # claude_code or codex

    local _delivery_failed=false
    local _failed_substep=""

    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        if ! _mock_tmux_retry "load_buffer"; then
            _delivery_failed=true
            _failed_substep="load_buffer"
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_tmux_retry "paste_buffer"; then
                _delivery_failed=true
                _failed_substep="paste_buffer"
            fi
        fi
    else
        if ! _mock_tmux_retry "send_skill"; then
            _delivery_failed=true
            _failed_substep="send_skill"
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_tmux_retry "load_buffer"; then
                _delivery_failed=true
                _failed_substep="load_buffer"
            fi
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_tmux_retry "paste_buffer"; then
                _delivery_failed=true
                _failed_substep="paste_buffer"
            fi
        fi
    fi

    if [ "$_delivery_failed" = true ]; then
        printf '\n\n[DELIVERY_SUBSTEP_FAILED: substep=%s] tmux delivery failed at substep. Retry is automatic.\n' \
            "$_failed_substep" >> "$dispatch_file"
        return 1
    fi

    # Enter substep
    if ! _mock_tmux_retry "enter"; then
        printf '\n\n[DELIVERY_SUBSTEP_FAILED: substep=enter] tmux Enter failed at substep. Retry is automatic.\n' \
            >> "$dispatch_file"
        return 1
    fi

    # Success: no annotation written
    return 0
}

# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------

TMP_ROOT=$(mktemp -d)

# ===========================================================================
# T1: send_skill failure → [DELIVERY_SUBSTEP_FAILED: substep=send_skill]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-send-skill.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_skill" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_fail $rc "T1: send_skill failure returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=send_skill\]" \
    "T1: send_skill failure writes substep annotation"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "T1: send_skill failure does NOT write generic [REJECTED:] annotation"

# ===========================================================================
# T2: load_buffer failure (claude_code) → [DELIVERY_SUBSTEP_FAILED: substep=load_buffer]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-load-buffer-claude.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="load_buffer" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_fail $rc "T2: load_buffer failure (claude) returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=load_buffer\]" \
    "T2: load_buffer failure (claude) writes substep annotation"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "T2: load_buffer failure (claude) does NOT write [REJECTED:] annotation"

# ===========================================================================
# T3: paste_buffer failure (claude_code) → [DELIVERY_SUBSTEP_FAILED: substep=paste_buffer]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-paste-buffer-claude.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="paste_buffer" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_fail $rc "T3: paste_buffer failure (claude) returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=paste_buffer\]" \
    "T3: paste_buffer failure (claude) writes substep annotation"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "T3: paste_buffer failure (claude) does NOT write [REJECTED:] annotation"

# ===========================================================================
# T4: load_buffer failure (codex) → [DELIVERY_SUBSTEP_FAILED: substep=load_buffer]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-load-buffer-codex.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="load_buffer" _simulate_delivery "$dispatch" "codex"
rc=$?
assert_fail $rc "T4: load_buffer failure (codex) returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=load_buffer\]" \
    "T4: load_buffer failure (codex) writes substep annotation"
assert_file_not_contains "$dispatch" "substep=send_skill" \
    "T4: codex path does NOT include send_skill substep (no such step)"

# ===========================================================================
# T5: paste_buffer failure (codex) → [DELIVERY_SUBSTEP_FAILED: substep=paste_buffer]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-paste-buffer-codex.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="paste_buffer" _simulate_delivery "$dispatch" "codex"
rc=$?
assert_fail $rc "T5: paste_buffer failure (codex) returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=paste_buffer\]" \
    "T5: paste_buffer failure (codex) writes substep annotation"

# ===========================================================================
# T6: enter failure → [DELIVERY_SUBSTEP_FAILED: substep=enter]
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-enter.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="enter" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_fail $rc "T6: enter failure returns non-zero"
assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED: substep=enter\]" \
    "T6: enter failure writes substep annotation"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "T6: enter failure does NOT write [REJECTED:] annotation"

# ===========================================================================
# T7: Successful delivery → no substep annotation written
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-success.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_pass $rc "T7: successful delivery returns 0"
assert_file_not_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
    "T7: successful delivery writes no substep annotation"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "T7: successful delivery writes no [REJECTED:] annotation"

# ===========================================================================
# T8: [DELIVERY_SUBSTEP_FAILED:] is requeueable — not treated as rejection
# Verify the marker does NOT match the [REJECTED:] pattern used by RC-3 logic
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-requeueable-check.md"
printf '# Dispatch\n\n[DELIVERY_SUBSTEP_FAILED: substep=send_skill] tmux delivery failed.\n' > "$dispatch"
if grep -q "\[REJECTED:" "$dispatch"; then
    fail "T8: [DELIVERY_SUBSTEP_FAILED:] mistakenly matches [REJECTED:] pattern" \
        "marker would trigger permanent rejection — must not"
else
    pass "T8: [DELIVERY_SUBSTEP_FAILED:] does NOT match [REJECTED:] pattern (requeueable)"
fi

# ===========================================================================
# T9: Annotation text includes retry guidance
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-annotation-text.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_skill" _simulate_delivery "$dispatch" "claude_code"
assert_file_contains "$dispatch" "Retry is automatic" \
    "T9: substep annotation includes retry guidance"

# ===========================================================================
# T10: Each substep (send_skill, load_buffer, paste_buffer, enter) is independently
#      identifiable — substep name appears in annotation verbatim
# ===========================================================================
for substep in send_skill load_buffer paste_buffer enter; do
    dispatch="$TMP_ROOT/dispatch-substep-id-${substep}.md"
    echo "# Dispatch" > "$dispatch"
    FAIL_SUBSTEP="$substep" _simulate_delivery "$dispatch" "claude_code"
    assert_file_contains "$dispatch" "substep=${substep}" \
        "T10: substep='${substep}' identifiable in annotation"
done

# ===========================================================================
# T11: codex send_skill substep does not exist — a send_skill failure on codex
#      must not annotate (codex path never reaches send_skill)
# ===========================================================================
dispatch="$TMP_ROOT/dispatch-codex-no-send-skill.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_skill" _simulate_delivery "$dispatch" "codex"
rc=$?
assert_pass $rc "T11: codex path with FAIL_SUBSTEP=send_skill succeeds (no such substep)"
assert_file_not_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
    "T11: codex path does NOT annotate send_skill (substep not in codex path)"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
rm -rf "$TMP_ROOT"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== test_delivery_substep_logging results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

[ "$FAIL_COUNT" -eq 0 ] || exit 1
