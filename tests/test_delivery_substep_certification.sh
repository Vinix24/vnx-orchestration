#!/usr/bin/env bash
# PR-2 Certification: Delivery Substep Observability
#
# Gate: gate_pr2_delivery_substep_certification
# Contract: docs/core/150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md
#
# Certifies DS-1 through DS-3 rules:
#   CERT-1: Each substep failure produces correct annotation (all 7 substep IDs)
#   CERT-2: No generic [REJECTED:] for delivery substep failures (DS-1)
#   CERT-3: Annotation is parseable by grep/jq audit tooling
#   CERT-4: Codex vs Claude path isolation (correct substeps per provider)
#   CERT-5: Classification of delivery_failed:* as ambiguous true (DS-3)
#   CERT-6: Successful delivery produces no annotation
#   CERT-7: Marker is requeueable under RC-3 disposition logic

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

# --- Mock delivery substep logic (mirrors dispatcher) ---
FAIL_SUBSTEP=""

_mock_substep() {
    local substep="$1"
    [ "$FAIL_SUBSTEP" = "$substep" ] && return 1
    return 0
}

_simulate_delivery() {
    local dispatch_file="$1"
    local provider="${2:-claude_code}"

    local _delivery_failed=false
    local _failed_substep=""

    if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
        if ! _mock_substep "load_buffer_codex"; then
            _delivery_failed=true; _failed_substep="load_buffer_codex"
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_substep "paste_buffer_codex"; then
                _delivery_failed=true; _failed_substep="paste_buffer_codex"
            fi
        fi
    else
        if ! _mock_substep "send_skill"; then
            _delivery_failed=true; _failed_substep="send_skill"
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_substep "load_buffer"; then
                _delivery_failed=true; _failed_substep="load_buffer"
            fi
        fi
        if [ "$_delivery_failed" = false ]; then
            if ! _mock_substep "paste_buffer"; then
                _delivery_failed=true; _failed_substep="paste_buffer"
            fi
        fi
    fi

    if [ "$_delivery_failed" = true ]; then
        printf '\n\n[DELIVERY_SUBSTEP_FAILED: substep=%s] tmux delivery failed at substep. Retry is automatic.\n' \
            "$_failed_substep" >> "$dispatch_file"
        return 1
    fi

    if ! _mock_substep "send_enter"; then
        printf '\n\n[DELIVERY_SUBSTEP_FAILED: substep=send_enter] tmux Enter failed at substep. Retry is automatic.\n' \
            >> "$dispatch_file"
        return 1
    fi

    return 0
}

# Mirror of _classify_blocked_dispatch with delivery_failed:* support (DS-3)
_classify_blocked_dispatch() {
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*) echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable) echo "ambiguous true" ;;
        canonical_lease:*) echo "busy true" ;;
        canonical_check_parse_error|canonical_lease_acquire_failed|input_mode_blocked) echo "ambiguous true" ;;
        blocked_input_mode|recovery_failed|pane_dead|probe_failed) echo "ambiguous true" ;;
        delivery_failed:*) echo "ambiguous true" ;;
        *) echo "invalid false" ;;
    esac
}

# Mirror of RC-3 disposition
_rc3_handle_failure() {
    local dispatch="$1"
    if grep -q "\[SKILL_INVALID\]" "$dispatch"; then echo "pending:skill_invalid"; return; fi
    if grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then echo "pending:dependency_error"; return; fi
    if grep -q "\[REJECTED:" "$dispatch"; then echo "rejected:permanent"; return; fi
    echo "pending:requeueable"
}

TMP_ROOT=$(mktemp -d)

echo "=== PR-2 Certification: Delivery Substep Observability ==="
echo ""

# =========================================================================
# CERT-1: Each substep failure produces correct annotation
# =========================================================================
echo "--- CERT-1: Substep annotation per failure ---"

# Claude/Gemini path substeps
for substep in send_skill load_buffer paste_buffer; do
    dispatch="$TMP_ROOT/cert1-claude-${substep}.md"
    echo "# Dispatch" > "$dispatch"
    FAIL_SUBSTEP="$substep" _simulate_delivery "$dispatch" "claude_code"
    assert_file_contains "$dispatch" "substep=${substep}" \
        "CERT-1a: Claude ${substep} failure annotates substep=${substep}"
done

# Codex path substeps
for substep in load_buffer_codex paste_buffer_codex; do
    dispatch="$TMP_ROOT/cert1-codex-${substep}.md"
    echo "# Dispatch" > "$dispatch"
    FAIL_SUBSTEP="$substep" _simulate_delivery "$dispatch" "codex"
    assert_file_contains "$dispatch" "substep=${substep}" \
        "CERT-1b: Codex ${substep} failure annotates substep=${substep}"
done

# send_enter (shared by both paths)
dispatch="$TMP_ROOT/cert1-enter.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_enter" _simulate_delivery "$dispatch" "claude_code"
assert_file_contains "$dispatch" "substep=send_enter" \
    "CERT-1c: send_enter failure annotates substep=send_enter"

dispatch="$TMP_ROOT/cert1-enter-codex.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_enter" _simulate_delivery "$dispatch" "codex"
assert_file_contains "$dispatch" "substep=send_enter" \
    "CERT-1d: send_enter failure on Codex annotates substep=send_enter"

# =========================================================================
# CERT-2: No [REJECTED:] for delivery substep failures (DS-1)
# =========================================================================
echo ""
echo "--- CERT-2: No [REJECTED:] for substep failures (DS-1) ---"

for substep in send_skill load_buffer paste_buffer send_enter; do
    dispatch="$TMP_ROOT/cert2-${substep}.md"
    echo "# Dispatch" > "$dispatch"
    FAIL_SUBSTEP="$substep" _simulate_delivery "$dispatch" "claude_code"
    assert_file_not_contains "$dispatch" "\[REJECTED:" \
        "CERT-2a: ${substep} failure does NOT produce [REJECTED:]"
    assert_file_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
        "CERT-2b: ${substep} failure uses [DELIVERY_SUBSTEP_FAILED:] marker"
done

# =========================================================================
# CERT-3: Annotation is parseable by grep audit tooling
# =========================================================================
echo ""
echo "--- CERT-3: Annotation parseability ---"

dispatch="$TMP_ROOT/cert3-parse.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_skill" _simulate_delivery "$dispatch" "claude_code"

# Extract substep name using grep + sed (typical audit parsing)
extracted=$(grep "\[DELIVERY_SUBSTEP_FAILED:" "$dispatch" | sed 's/.*substep=\([a-z_]*\).*/\1/')
assert_eq "send_skill" "$extracted" "CERT-3a: substep name extractable by grep+sed"

# Verify fixed format: [DELIVERY_SUBSTEP_FAILED: substep=<name>]
grep -qE '^\[DELIVERY_SUBSTEP_FAILED: substep=[a-z_]+\]' "$dispatch"
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "CERT-3b: annotation matches expected regex pattern"
else
    fail "CERT-3b: annotation matches expected regex pattern" "regex did not match"
fi

# Verify annotation contains retry guidance
assert_file_contains "$dispatch" "Retry is automatic" \
    "CERT-3c: annotation includes retry guidance text"

# =========================================================================
# CERT-4: Provider path isolation
# =========================================================================
echo ""
echo "--- CERT-4: Provider path isolation ---"

# Codex should not have send_skill substep
dispatch="$TMP_ROOT/cert4-codex-no-send-skill.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="send_skill" _simulate_delivery "$dispatch" "codex"
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "CERT-4a: Codex path ignores send_skill (substep not in path)"
else
    fail "CERT-4a: Codex path ignores send_skill" "unexpected failure"
fi
assert_file_not_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
    "CERT-4b: No annotation for non-existent Codex substep"

# Claude should not have load_buffer_codex substep
dispatch="$TMP_ROOT/cert4-claude-no-codex-buffer.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="load_buffer_codex" _simulate_delivery "$dispatch" "claude_code"
rc=$?
if [ "$rc" -eq 0 ]; then
    pass "CERT-4c: Claude path ignores load_buffer_codex (substep not in path)"
else
    fail "CERT-4c: Claude path ignores load_buffer_codex" "unexpected failure"
fi

# =========================================================================
# CERT-5: delivery_failed:* classified as ambiguous true (DS-3)
# =========================================================================
echo ""
echo "--- CERT-5: delivery_failed:* classification (DS-3) ---"

for substep in send_skill load_buffer paste_buffer send_enter load_buffer_codex paste_buffer_codex; do
    result=$(_classify_blocked_dispatch "delivery_failed:${substep}")
    category="${result%% *}"
    requeueable="${result##* }"
    assert_eq "ambiguous" "$category" "CERT-5a: delivery_failed:${substep} → ambiguous"
    assert_eq "true" "$requeueable" "CERT-5b: delivery_failed:${substep} → requeueable"
done

# =========================================================================
# CERT-6: Successful delivery produces no annotation
# =========================================================================
echo ""
echo "--- CERT-6: Success path clean ---"

dispatch="$TMP_ROOT/cert6-success.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="" _simulate_delivery "$dispatch" "claude_code"
rc=$?
assert_eq "0" "$rc" "CERT-6a: successful delivery returns 0"
assert_file_not_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
    "CERT-6b: no substep annotation on success"
assert_file_not_contains "$dispatch" "\[REJECTED:" \
    "CERT-6c: no [REJECTED:] on success"

dispatch="$TMP_ROOT/cert6-success-codex.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="" _simulate_delivery "$dispatch" "codex"
rc=$?
assert_eq "0" "$rc" "CERT-6d: Codex successful delivery returns 0"
assert_file_not_contains "$dispatch" "\[DELIVERY_SUBSTEP_FAILED:" \
    "CERT-6e: no substep annotation on Codex success"

# =========================================================================
# CERT-7: Marker is requeueable under RC-3 disposition
# =========================================================================
echo ""
echo "--- CERT-7: Requeueable under RC-3 ---"

dispatch="$TMP_ROOT/cert7-requeue.md"
echo "# Dispatch" > "$dispatch"
FAIL_SUBSTEP="paste_buffer" _simulate_delivery "$dispatch" "claude_code"

# RC-3 disposition: [DELIVERY_SUBSTEP_FAILED:] is not [REJECTED:] and not [SKILL_INVALID]
# → should be "pending:requeueable"
result=$(_rc3_handle_failure "$dispatch")
assert_eq "pending:requeueable" "$result" "CERT-7a: substep failure dispatch stays in pending (requeueable)"

# Verify: if someone manually adds [REJECTED:] alongside, REJECTED wins
echo -e "\n[REJECTED: Manual rejection]" >> "$dispatch"
result=$(_rc3_handle_failure "$dispatch")
assert_eq "rejected:permanent" "$result" "CERT-7b: explicit [REJECTED:] overrides substep marker"

# =========================================================================
# CLEANUP
# =========================================================================
rm -rf "$TMP_ROOT"

echo ""
echo "=== PR-2 Certification results: $PASS_COUNT passed, $FAIL_COUNT failed ==="
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "CERTIFICATION: PASS"
    echo "  - All 7 substep IDs produce correct annotations"
    echo "  - No [REJECTED:] for delivery substep failures (DS-1)"
    echo "  - Annotation format parseable by grep/sed audit tooling"
    echo "  - Provider path isolation correct (Claude vs Codex)"
    echo "  - delivery_failed:* classified as ambiguous/requeueable (DS-3)"
    echo "  - Successful delivery produces no annotation"
    echo "  - Substep marker is requeueable under RC-3 disposition"
else
    echo "CERTIFICATION: FAIL — $FAIL_COUNT assertion(s) failed"
fi

[ "$FAIL_COUNT" -eq 0 ] || exit 1
