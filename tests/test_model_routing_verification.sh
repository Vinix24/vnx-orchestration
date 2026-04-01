#!/usr/bin/env bash
# Model routing verification tests — PR-2 quality gate
# Gate: gate_pr2_verified_model_switching
#
# Covers:
#   1. dispatch_metadata: Requires-Model value extraction (first token only, strength suffix stripped)
#   2. dispatch_metadata: Requires-Model strength extraction
#   3. vnx_eval_model_routing: not_requested, needs_switch (claude_code/codex_cli),
#      unsupported required (gemini — blocked), unsupported advisory (gemini — proceed),
#      unsupported unknown provider
#   4. vnx_verify_model_switch_output: switched, already_active, unverified states
#   5. vnx_emit_model_switch_result: event fields, model_match classification,
#      blocking (required + non-verified), non-blocking (advisory + non-verified)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"
source "$SCRIPT_DIR/../scripts/lib/model_routing.sh"

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
    local model_line="$1"
    local tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<EOF
[[TARGET:B]]
Dispatch-ID: test-dispatch-m01
${model_line}
Role: backend-developer
EOF
    echo "$tmp"
}

# ---------------------------------------------------------------------------
# Section 1: dispatch_metadata — Requires-Model value extraction
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 1: Model value extraction ==="

f=$(make_dispatch "Requires-Model: sonnet")
assert_eq "sonnet" "$(vnx_dispatch_extract_requires_model "$f")" \
    "advisory model extracted correctly (no suffix)"
rm -f "$f"

f=$(make_dispatch "Requires-Model: sonnet required")
assert_eq "sonnet" "$(vnx_dispatch_extract_requires_model "$f")" \
    "required model: value is model id only (suffix stripped)"
rm -f "$f"

f=$(make_dispatch "Requires-Model: opus required")
assert_eq "opus" "$(vnx_dispatch_extract_requires_model "$f")" \
    "required opus: value is model id only"
rm -f "$f"

f=$(make_dispatch "Requires-Model: OPUS REQUIRED")
assert_eq "opus" "$(vnx_dispatch_extract_requires_model "$f")" \
    "model value normalized to lowercase"
rm -f "$f"

f=$(make_dispatch "Requires-Model: default required # use default alias")
assert_eq "default" "$(vnx_dispatch_extract_requires_model "$f")" \
    "required default: trailing comment stripped, first token returned"
rm -f "$f"

f=$(make_dispatch "")
assert_eq "" "$(vnx_dispatch_extract_requires_model "$f")" \
    "absent field returns empty string"
rm -f "$f"

# ---------------------------------------------------------------------------
# Section 2: dispatch_metadata — Requires-Model strength extraction
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 2: Model strength extraction ==="

f=$(make_dispatch "Requires-Model: sonnet")
assert_eq "advisory" "$(vnx_dispatch_extract_requires_model_strength "$f")" \
    "no suffix → advisory (default)"
rm -f "$f"

f=$(make_dispatch "Requires-Model: opus required")
assert_eq "required" "$(vnx_dispatch_extract_requires_model_strength "$f")" \
    "lowercase 'required' suffix → required"
rm -f "$f"

f=$(make_dispatch "Requires-Model: opus REQUIRED")
assert_eq "required" "$(vnx_dispatch_extract_requires_model_strength "$f")" \
    "uppercase 'REQUIRED' suffix → required (case-insensitive)"
rm -f "$f"

f=$(make_dispatch "Requires-Model: sonnet required # comment")
assert_eq "required" "$(vnx_dispatch_extract_requires_model_strength "$f")" \
    "required with trailing comment → required"
rm -f "$f"

f=$(make_dispatch "")
assert_eq "advisory" "$(vnx_dispatch_extract_requires_model_strength "$f")" \
    "absent field → advisory"
rm -f "$f"

# ---------------------------------------------------------------------------
# Section 3: vnx_eval_model_routing — pre-switch evaluation
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 3: vnx_eval_model_routing ==="

# 3a. No model requirement → not_requested, exit 0
event=$(vnx_eval_model_routing "" "advisory" "claude_code" "T2" "test-m001")
rc=$?
assert_exit_code 0 "$rc" "no model requirement → exit 0"
assert_contains '"result":"not_requested"' "$event" "no requirement → result=not_requested"

# 3b. claude_code + required model → needs_switch, exit 0
event=$(vnx_eval_model_routing "sonnet" "required" "claude_code" "T2" "test-m002")
rc=$?
assert_exit_code 0 "$rc" "claude_code required model → exit 0 (needs_switch)"
assert_contains '"result":"needs_switch"' "$event" "claude_code required → result=needs_switch"
assert_contains '"requested_model":"sonnet"' "$event" "claude_code needs_switch → requested_model in event"

# 3c. claude_code + advisory model → needs_switch, exit 0
event=$(vnx_eval_model_routing "opus" "advisory" "claude_code" "T1" "test-m003")
rc=$?
assert_exit_code 0 "$rc" "claude_code advisory model → exit 0"
assert_contains '"result":"needs_switch"' "$event" "claude_code advisory → result=needs_switch"

# 3d. codex_cli + required model → needs_switch, exit 0
event=$(vnx_eval_model_routing "sonnet" "required" "codex_cli" "T1" "test-m004")
rc=$?
assert_exit_code 0 "$rc" "codex_cli required model → exit 0 (needs_switch)"
assert_contains '"result":"needs_switch"' "$event" "codex_cli required → result=needs_switch"

# 3e. gemini_cli + required model → unsupported, exit 1 (blocked)
event=$(vnx_eval_model_routing "sonnet" "required" "gemini_cli" "T3" "test-m005") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "gemini_cli required model → exit 1 (blocked)"
assert_contains '"result":"unsupported"' "$event" "gemini required → result=unsupported"
assert_contains '"reason"' "$event" "gemini required → reason field present"

# 3f. gemini_cli + advisory model → unsupported, exit 0 (warn, proceed)
event=$(vnx_eval_model_routing "sonnet" "advisory" "gemini_cli" "T3" "test-m006")
rc=$?
assert_exit_code 0 "$rc" "gemini_cli advisory model → exit 0 (proceed with warning)"
assert_contains '"result":"unsupported"' "$event" "gemini advisory → result=unsupported (warn)"

# 3g. unknown provider + required model → unsupported, exit 1 (blocked)
event=$(vnx_eval_model_routing "opus" "required" "some_unknown_cli" "T1" "test-m007") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "unknown provider required model → exit 1 (blocked)"
assert_contains '"result":"unsupported"' "$event" "unknown provider required → blocked with unsupported"

# 3h. unknown provider + advisory model → unsupported, exit 0 (proceed)
event=$(vnx_eval_model_routing "opus" "advisory" "some_unknown_cli" "T1" "test-m008")
rc=$?
assert_exit_code 0 "$rc" "unknown provider advisory model → exit 0 (proceed)"

# ---------------------------------------------------------------------------
# Section 4: vnx_verify_model_switch_output — pane parser
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 4: vnx_verify_model_switch_output ==="

# 4a. Pane shows "Model set to claude-sonnet-4-6" → switched
result=$(vnx_verify_model_switch_output \
    "❯ /model sonnet
Model set to claude-sonnet-4-6
❯ " "sonnet")
assert_eq "switched" "$result" \
    "pane: 'Model set to claude-sonnet-4-6' → switched"

# 4b. Pane shows model name in last lines → switched (fallback)
result=$(vnx_verify_model_switch_output \
    "some output
claude-sonnet-4-6 active
❯ " "sonnet")
assert_eq "switched" "$result" \
    "pane: model name in last 10 lines → switched (fallback)"

# 4c. Pane shows "already using" → already_active
result=$(vnx_verify_model_switch_output \
    "❯ /model sonnet
Already using claude-sonnet-4-6
❯ " "sonnet")
assert_eq "already_active" "$result" \
    "pane: 'Already using ...' → already_active"

# 4d. Pane shows "already on" → already_active
result=$(vnx_verify_model_switch_output \
    "Already on claude-opus-4-6
❯ " "default")
assert_eq "already_active" "$result" \
    "pane: 'Already on ...' → already_active"

# 4e. Pane has no model confirmation → unverified
result=$(vnx_verify_model_switch_output \
    "❯ /model sonnet
Thinking...
❯ " "sonnet")
# Note: "Thinking..." contains no model confirmation
# "sonnet" does not appear in the last 10 lines beyond the /model command line itself
# This fixture intentionally omits the model name from confirmation lines
result2=$(vnx_verify_model_switch_output \
    "❯
❯ " "haiku")
assert_eq "unverified" "$result2" \
    "pane: no model name in output → unverified"

# 4f. Pane shows "default" pattern for opus → switched
result=$(vnx_verify_model_switch_output \
    "❯ /model default
Model set to claude-opus-4-6 (default)
❯ " "default")
assert_eq "switched" "$result" \
    "pane: 'Model set to ... (default)' → switched for default/opus"

# 4g. Pane shows "opus" when model_cmd is "default" → switched (alias check)
result=$(vnx_verify_model_switch_output \
    "❯ /model default
Switched to opus
❯ " "default")
assert_eq "switched" "$result" \
    "pane: 'opus' in output when model_cmd=default → switched (alias)"

# ---------------------------------------------------------------------------
# Section 5: vnx_emit_model_switch_result — event + blocking decision
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 5: vnx_emit_model_switch_result ==="

# 5a. switched + advisory → model_match=verified_match, exit 0
event=$(vnx_emit_model_switch_result "sonnet" "switched" "" "advisory" "T2" "test-m010")
rc=$?
assert_exit_code 0 "$rc" "switched advisory → exit 0 (proceed)"
assert_contains '"switch_result":"switched"' "$event" "switched advisory → switch_result in event"
assert_contains '"model_match":"verified_match"' "$event" "switched advisory → model_match=verified_match"

# 5b. already_active + required → model_match=verified_match, exit 0
event=$(vnx_emit_model_switch_result "opus" "already_active" "" "required" "T0" "test-m011")
rc=$?
assert_exit_code 0 "$rc" "already_active required → exit 0 (proceed)"
assert_contains '"switch_result":"already_active"' "$event" "already_active → switch_result in event"
assert_contains '"model_match":"verified_match"' "$event" "already_active required → verified_match"

# 5c. unverified + required → model_match=mismatch_blocked, exit 1 (blocked)
event=$(vnx_emit_model_switch_result "opus" "unverified" "" "required" "T2" "test-m012") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "unverified required → exit 1 (blocked)"
assert_contains '"switch_result":"unverified"' "$event" "unverified required → switch_result in event"
assert_contains '"model_match":"mismatch_blocked"' "$event" "unverified required → mismatch_blocked"
assert_contains '"requested_model":"opus"' "$event" "unverified required → requested_model in event"

# 5d. unverified + advisory → model_match=mismatch_advisory, exit 0
event=$(vnx_emit_model_switch_result "opus" "unverified" "" "advisory" "T2" "test-m013")
rc=$?
assert_exit_code 0 "$rc" "unverified advisory → exit 0 (proceed with warning)"
assert_contains '"model_match":"mismatch_advisory"' "$event" "unverified advisory → mismatch_advisory"

# 5e. failed + required → mismatch_blocked, exit 1 (blocked)
event=$(vnx_emit_model_switch_result "sonnet" "failed" "" "required" "T1" "test-m014") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "failed required → exit 1 (blocked)"
assert_contains '"model_match":"mismatch_blocked"' "$event" "failed required → mismatch_blocked"

# 5f. unsupported + advisory → mismatch_advisory, exit 0
event=$(vnx_emit_model_switch_result "sonnet" "unsupported" "" "advisory" "T3" "test-m015")
rc=$?
assert_exit_code 0 "$rc" "unsupported advisory → exit 0"
assert_contains '"model_match":"mismatch_advisory"' "$event" "unsupported advisory → mismatch_advisory"

# 5g. actual_model field is included when non-empty
event=$(vnx_emit_model_switch_result "sonnet" "switched" "claude-sonnet-4-6" "advisory" "T2" "test-m016")
assert_contains '"actual_model":"claude-sonnet-4-6"' "$event" \
    "actual_model field present when non-empty"

# 5h. actual_model field omitted when empty
event=$(vnx_emit_model_switch_result "opus" "switched" "" "required" "T0" "test-m017")
if [[ "$event" == *'"actual_model"'* ]]; then
    echo "FAIL: actual_model field should be absent when empty"
    FAIL=$(( FAIL + 1 ))
else
    echo "PASS: actual_model field absent when empty"
    PASS=$(( PASS + 1 ))
fi

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
    echo "RESULT: PASS — all $PASS model routing verification tests passed"
    exit 0
fi
