#!/usr/bin/env bash
# Provider routing enforcement tests — PR-1 quality gate
# Gate: gate_pr1_provider_enforcement
#
# Covers:
#   1. dispatch_metadata: provider value extraction (with and without 'required' suffix)
#   2. dispatch_metadata: provider strength extraction
#   3. vnx_eval_provider_routing: required match, required mismatch (blocked),
#      advisory match, advisory mismatch (warned), absent requirement

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"
source "$SCRIPT_DIR/../scripts/lib/provider_routing.sh"

PASS=0
FAIL=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
    if [[ "$expected" != "$actual" ]]; then
        echo "FAIL: $msg"
        echo "      expected='$expected'"
        echo "      actual  ='$actual'"
        FAIL=$(( FAIL + 1 ))
    else
        echo "PASS: $msg"
        PASS=$(( PASS + 1 ))
    fi
}

assert_exit_code() {
    local expected_code="$1"
    local actual_code="$2"
    local msg="$3"
    if [[ "$expected_code" != "$actual_code" ]]; then
        echo "FAIL: $msg"
        echo "      expected exit=$expected_code actual exit=$actual_code"
        FAIL=$(( FAIL + 1 ))
    else
        echo "PASS: $msg"
        PASS=$(( PASS + 1 ))
    fi
}

assert_contains() {
    local needle="$1"
    local haystack="$2"
    local msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        echo "FAIL: $msg"
        echo "      expected to contain: '$needle'"
        echo "      got: '$haystack'"
        FAIL=$(( FAIL + 1 ))
    else
        echo "PASS: $msg"
        PASS=$(( PASS + 1 ))
    fi
}

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

make_dispatch() {
    local provider_line="$1"
    local tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<EOF
[[TARGET:B]]
Dispatch-ID: test-dispatch-001
${provider_line}
Role: backend-developer
EOF
    echo "$tmp"
}

# ---------------------------------------------------------------------------
# Section 1: dispatch_metadata — provider value extraction
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 1: Provider value extraction ==="

f=$(make_dispatch "Requires-Provider: claude_code")
assert_eq "claude_code" "$(vnx_dispatch_extract_requires_provider "$f")" \
    "advisory provider extracted correctly (no suffix)"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: claude_code required")
assert_eq "claude_code" "$(vnx_dispatch_extract_requires_provider "$f")" \
    "required provider: value is provider id only (suffix stripped)"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: codex_cli required")
assert_eq "codex_cli" "$(vnx_dispatch_extract_requires_provider "$f")" \
    "required codex_cli: value is provider id only"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: GEMINI_CLI required")
assert_eq "gemini_cli" "$(vnx_dispatch_extract_requires_provider "$f")" \
    "provider value normalized to lowercase"
rm -f "$f"

f=$(make_dispatch "")
assert_eq "" "$(vnx_dispatch_extract_requires_provider "$f")" \
    "absent field returns empty string"
rm -f "$f"

# ---------------------------------------------------------------------------
# Section 2: dispatch_metadata — strength extraction
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 2: Provider strength extraction ==="

f=$(make_dispatch "Requires-Provider: claude_code")
assert_eq "advisory" "$(vnx_dispatch_extract_requires_provider_strength "$f")" \
    "no suffix → advisory (default)"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: claude_code required")
assert_eq "required" "$(vnx_dispatch_extract_requires_provider_strength "$f")" \
    "lowercase 'required' suffix → required"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: claude_code REQUIRED")
assert_eq "required" "$(vnx_dispatch_extract_requires_provider_strength "$f")" \
    "uppercase 'REQUIRED' suffix → required (case-insensitive)"
rm -f "$f"

f=$(make_dispatch "Requires-Provider: codex_cli required # comment")
assert_eq "required" "$(vnx_dispatch_extract_requires_provider_strength "$f")" \
    "required with trailing comment → required"
rm -f "$f"

f=$(make_dispatch "")
assert_eq "advisory" "$(vnx_dispatch_extract_requires_provider_strength "$f")" \
    "absent field → advisory"
rm -f "$f"

# ---------------------------------------------------------------------------
# Section 3: vnx_eval_provider_routing — enforcement logic
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 3: Provider routing enforcement ==="

# 3a. Required — match → proceed (exit 0)
event=$(vnx_eval_provider_routing "claude_code" "required" "claude_code" "T2" "test-dispatch-001")
rc=$?
assert_exit_code 0 "$rc" "required match → exit 0 (proceed)"
assert_contains '"result":"match"' "$event" "required match → result=match in event"

# 3b. Required — mismatch → block (exit 1)
event=$(vnx_eval_provider_routing "codex_cli" "required" "claude_code" "T2" "test-dispatch-002") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "required mismatch → exit 1 (blocked)"
assert_contains '"result":"mismatch_blocked"' "$event" "required mismatch → result=mismatch_blocked in event"
assert_contains '"reason":"required provider mismatch"' "$event" "required mismatch → explicit reason in event"
assert_contains '"requested_provider":"codex_cli"' "$event" "required mismatch → requested provider in event"
assert_contains '"actual_provider":"claude_code"' "$event" "required mismatch → actual provider in event"

# 3c. Advisory — match → proceed (exit 0)
event=$(vnx_eval_provider_routing "claude_code" "advisory" "claude_code" "T1" "test-dispatch-003")
rc=$?
assert_exit_code 0 "$rc" "advisory match → exit 0 (proceed)"
assert_contains '"result":"match"' "$event" "advisory match → result=match in event"

# 3d. Advisory — mismatch → warn and proceed (exit 0)
event=$(vnx_eval_provider_routing "codex_cli" "advisory" "claude_code" "T1" "test-dispatch-004")
rc=$?
assert_exit_code 0 "$rc" "advisory mismatch → exit 0 (proceed)"
assert_contains '"result":"mismatch_advisory"' "$event" "advisory mismatch → result=mismatch_advisory in event"
assert_contains '"requested_provider":"codex_cli"' "$event" "advisory mismatch → requested provider in event"
assert_contains '"actual_provider":"claude_code"' "$event" "advisory mismatch → actual provider in event"

# 3e. No requirement → proceed (exit 0)
event=$(vnx_eval_provider_routing "" "advisory" "claude_code" "T2" "test-dispatch-005")
rc=$?
assert_exit_code 0 "$rc" "no provider requirement → exit 0 (proceed)"
assert_contains '"result":"not_required"' "$event" "no requirement → result=not_required in event"

# 3f. Required — gemini vs claude → block (cross-provider)
event=$(vnx_eval_provider_routing "gemini_cli" "required" "claude_code" "T3" "test-dispatch-006") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "required gemini_cli on claude_code terminal → blocked"
assert_contains '"result":"mismatch_blocked"' "$event" "gemini vs claude required mismatch → blocked event"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo "RESULT: FAIL ($FAIL test(s) failed)"
    exit 1
else
    echo "RESULT: PASS — all $PASS provider routing enforcement tests passed"
    exit 0
fi
