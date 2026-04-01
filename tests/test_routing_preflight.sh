#!/usr/bin/env bash
# Routing preflight readiness tests — PR-3 quality gate
# Gate: gate_pr3_routing_preflight_readiness
#
# Covers:
#   Section 1: Provider readiness checks (ready, misconfigured, unsupported)
#   Section 2: Model readiness checks (ready, ready_with_switch, unsupported)
#   Section 3: Pinned assumption verification (verified, drift)
#   Section 4: Full chain readiness (combined provider + model)
#   Section 5: Routing state classification (unsupported, unavailable, misconfigured)
#   Section 6: Preset diagnostics

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../scripts/lib/routing_preflight.sh"

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
# Clean env to avoid test pollution — unset any provider/model env vars
# ---------------------------------------------------------------------------
_clean_env() {
    unset VNX_T0_PROVIDER VNX_T1_PROVIDER VNX_T2_PROVIDER VNX_T3_PROVIDER 2>/dev/null || true
    unset VNX_T0_MODEL VNX_T1_MODEL VNX_T2_MODEL VNX_T3_MODEL 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Section 1: Provider readiness checks
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 1: Provider readiness checks ==="

_clean_env

# 1a. No provider requirement → ready
event=$(vnx_check_provider_readiness "T1" "" "advisory")
rc=$?
assert_exit_code 0 "$rc" "no provider requirement → exit 0"
assert_contains '"result":"not_required"' "$event" "no requirement → not_required event"

# 1b. Default provider matches (T1 = claude_code by default) → ready
event=$(vnx_check_provider_readiness "T1" "claude_code" "required")
rc=$?
assert_exit_code 0 "$rc" "default provider matches → exit 0"
assert_contains '"result":"ready"' "$event" "matching provider → ready"

# 1c. Provider mismatch, required → blocked
event=$(vnx_check_provider_readiness "T1" "codex_cli" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "required provider mismatch → exit 1"
assert_contains '"result":"not_ready"' "$event" "mismatched required provider → not_ready"
assert_contains '"gap":"misconfigured"' "$event" "known provider mismatch → misconfigured"

# 1d. Provider mismatch, advisory → proceeds with warning
event=$(vnx_check_provider_readiness "T1" "codex_cli" "advisory")
rc=$?
assert_exit_code 0 "$rc" "advisory provider mismatch → exit 0"
assert_contains '"result":"not_ready"' "$event" "mismatched advisory provider → not_ready (but proceeds)"

# 1e. Unknown provider → unsupported gap
event=$(vnx_check_provider_readiness "T1" "unknown_provider" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "unsupported provider required → exit 1"
assert_contains '"gap":"unsupported"' "$event" "unknown provider → unsupported gap"

# 1f. Env var override changes provider → match
export VNX_T1_PROVIDER=codex_cli
event=$(vnx_check_provider_readiness "T1" "codex_cli" "required")
rc=$?
assert_exit_code 0 "$rc" "env override makes codex_cli ready → exit 0"
assert_contains '"result":"ready"' "$event" "env override provider → ready"
_clean_env

# ---------------------------------------------------------------------------
# Section 2: Model readiness checks
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 2: Model readiness checks ==="

_clean_env

# 2a. No model requirement → ready
event=$(vnx_check_model_readiness "T1" "" "advisory")
rc=$?
assert_exit_code 0 "$rc" "no model requirement → exit 0"
assert_contains '"result":"not_required"' "$event" "no model requirement → not_required"

# 2b. Pinned model matches (T1 = sonnet by default) → ready
event=$(vnx_check_model_readiness "T1" "sonnet" "required")
rc=$?
assert_exit_code 0 "$rc" "pinned model matches → exit 0"
assert_contains '"result":"ready"' "$event" "pinned model match → ready"

# 2c. T0 pinned to default, requesting opus → ready (opus == default)
event=$(vnx_check_model_readiness "T0" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "opus == default (T0 pinned) → exit 0"
assert_contains '"result":"ready"' "$event" "opus normalizes to default → ready"

# 2d. Model mismatch but provider supports switching → ready_with_switch
event=$(vnx_check_model_readiness "T1" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "model mismatch, claude_code supports /model → exit 0"
assert_contains '"result":"ready_with_switch"' "$event" "switchable provider → ready_with_switch"

# 2e. Model mismatch on non-switchable provider → not_ready
export VNX_T1_PROVIDER=gemini_cli
event=$(vnx_check_model_readiness "T1" "opus" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "model mismatch on gemini (no /model) required → exit 1"
assert_contains '"result":"not_ready"' "$event" "gemini cannot switch → not_ready"
assert_contains '"gap":"unsupported"' "$event" "gemini model switch → unsupported gap"
_clean_env

# 2f. Model mismatch on non-switchable, advisory → proceeds
export VNX_T1_PROVIDER=gemini_cli
event=$(vnx_check_model_readiness "T1" "opus" "advisory")
rc=$?
assert_exit_code 0 "$rc" "model mismatch on gemini advisory → exit 0 (warn)"
_clean_env

# 2g. Env var model override → matches
export VNX_T1_MODEL=opus
event=$(vnx_check_model_readiness "T1" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "env model override matches → exit 0"
assert_contains '"result":"ready"' "$event" "env model override → ready"
_clean_env

# ---------------------------------------------------------------------------
# Section 3: Pinned assumption verification
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 3: Pinned assumption verification ==="

_clean_env

# 3a. Default env → all assumptions verified
events=$(vnx_check_pinned_assumptions)
rc=$?
assert_exit_code 0 "$rc" "default env → all pinned assumptions hold → exit 0"
assert_contains '"result":"verified"' "$events" "default env → verified events present"
# Count verified lines
verified_count=$(echo "$events" | grep -c '"result":"verified"' || true)
assert_eq "4" "$verified_count" "all 4 terminals verified"

# 3b. Provider drift → detected
export VNX_T1_PROVIDER=codex_cli
events=$(vnx_check_pinned_assumptions) || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "T1 provider drift → exit 1"
assert_contains '"result":"drift"' "$events" "provider drift → drift event"
assert_contains '"terminal":"T1"' "$events" "drift on T1"
_clean_env

# 3c. Model drift → detected
export VNX_T2_MODEL=opus
events=$(vnx_check_pinned_assumptions) || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "T2 model drift → exit 1"
# T2 pinned=sonnet but env says opus, so drift
drift_events=$(echo "$events" | grep '"result":"drift"' || true)
assert_contains "T2" "$drift_events" "model drift on T2 detected"
_clean_env

# ---------------------------------------------------------------------------
# Section 4: Full chain readiness (combined checks)
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 4: Full chain readiness ==="

_clean_env

# 4a. No requirements → passes
events=$(vnx_preflight_routing_readiness "T1" "" "advisory" "" "advisory")
rc=$?
assert_exit_code 0 "$rc" "no requirements → chain ready"

# 4b. Provider + model both match → passes
events=$(vnx_preflight_routing_readiness "T1" "claude_code" "required" "sonnet" "required")
rc=$?
assert_exit_code 0 "$rc" "provider + model match → chain ready"

# 4c. Provider mismatch required → blocks
events=$(vnx_preflight_routing_readiness "T1" "codex_cli" "required" "sonnet" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "required provider mismatch → chain blocked"

# 4d. Model mismatch on non-switchable required → blocks
export VNX_T1_PROVIDER=gemini_cli
events=$(vnx_preflight_routing_readiness "T1" "gemini_cli" "required" "opus" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "required model on non-switchable → chain blocked"
_clean_env

# 4e. Advisory mismatches → passes with warnings
events=$(vnx_preflight_routing_readiness "T1" "codex_cli" "advisory" "opus" "advisory")
rc=$?
assert_exit_code 0 "$rc" "advisory mismatches → chain passes"

# ---------------------------------------------------------------------------
# Section 5: Routing state classification
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 5: Routing state classification ==="

_clean_env

# T0 can distinguish: unsupported, unavailable, misconfigured

# 5a. Unsupported: unknown provider
event=$(vnx_check_provider_readiness "T1" "llama_cli" "required") || rc=$?
rc=${rc:-0}
assert_contains '"gap":"unsupported"' "$event" "unknown provider → unsupported"

# 5b. Misconfigured: known provider, wrong terminal
event=$(vnx_check_provider_readiness "T1" "gemini_cli" "required") || rc=$?
rc=${rc:-0}
assert_contains '"gap":"misconfigured"' "$event" "known provider wrong terminal → misconfigured"

# 5c. Unsupported model switch: gemini cannot switch
export VNX_T2_PROVIDER=gemini_cli
event=$(vnx_check_model_readiness "T2" "opus" "required") || rc=$?
rc=${rc:-0}
assert_contains '"gap":"unsupported"' "$event" "gemini model switch → unsupported"
_clean_env

# 5d. Model readiness with switch capability → no gap
event=$(vnx_check_model_readiness "T1" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "claude_code can switch → ready (no gap)"
# Verify no gap field in the output for ready_with_switch
assert_contains '"result":"ready_with_switch"' "$event" "switch-capable → ready_with_switch"

# ---------------------------------------------------------------------------
# Section 6: Preset diagnostics
# ---------------------------------------------------------------------------
echo ""
echo "=== Section 6: Preset diagnostics ==="

_clean_env

# Create temp preset files for testing
PRESET_DIR=$(mktemp -d)

cat > "$PRESET_DIR/test-claude.env" <<'EOF'
VNX_T1_PROVIDER=claude_code
VNX_T2_PROVIDER=claude_code
EOF

cat > "$PRESET_DIR/test-codex.env" <<'EOF'
VNX_T1_PROVIDER=codex_cli
VNX_T2_PROVIDER=claude_code
EOF

# 6a. Claude preset satisfies claude_code requirement
event=$(vnx_preflight_preset_diagnostics "$PRESET_DIR/test-claude.env" "T1" "claude_code" "")
rc=$?
assert_exit_code 0 "$rc" "claude preset satisfies claude_code → exit 0"
assert_contains '"result":"ready"' "$event" "preset ready for claude_code"

# 6b. Claude preset does NOT satisfy codex_cli requirement
event=$(vnx_preflight_preset_diagnostics "$PRESET_DIR/test-claude.env" "T1" "codex_cli" "") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "claude preset cannot satisfy codex_cli → exit 1"
assert_contains '"result":"not_ready"' "$event" "preset not ready for codex_cli"

# 6c. Codex preset satisfies codex_cli requirement
event=$(vnx_preflight_preset_diagnostics "$PRESET_DIR/test-codex.env" "T1" "codex_cli" "")
rc=$?
assert_exit_code 0 "$rc" "codex preset satisfies codex_cli → exit 0"
assert_contains '"result":"ready"' "$event" "codex preset ready"

# 6d. Non-existent preset → error
event=$(vnx_preflight_preset_diagnostics "/nonexistent/preset.env" "T1" "claude_code" "") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "missing preset file → exit 1"
assert_contains '"result":"error"' "$event" "missing preset → error event"

# Cleanup
rm -rf "$PRESET_DIR"

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
    echo "RESULT: PASS — all $PASS routing preflight readiness tests passed"
    exit 0
fi
