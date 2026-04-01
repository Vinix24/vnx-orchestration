#!/usr/bin/env bash
# PR-4: Routing Certification — Mixed-Provider and Mixed-Model Scenarios
# Gate: gate_pr4_routing_certification
#
# Certifies the full routing enforcement stack using realistic dispatch scenarios.
# Exercises PR-1 (provider enforcement), PR-2 (model verification), and PR-3
# (preflight readiness) together to prove deterministic routing behavior.
#
# Sections:
#   1. Mixed-provider: required codex_cli on claude_code terminal → blocked
#   2. Mixed-provider: advisory codex_cli on claude_code → warned, proceeds
#   3. Mixed-model: required opus on sonnet-pinned terminal → switch capable
#   4. Mixed-model: required opus on gemini terminal → blocked (no /model)
#   5. Full dispatch lifecycle: metadata → preflight → enforcement
#   6. Evidence completeness: all routing events contain identity fields
#   7. Pinned-terminal assumptions: explicit check before chain start
#   8. Cross-layer consistency: shell and Python agree on readiness

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"
source "$SCRIPT_DIR/../scripts/lib/provider_routing.sh"
source "$SCRIPT_DIR/../scripts/lib/model_routing.sh"
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

_clean_env() {
    unset VNX_T0_PROVIDER VNX_T1_PROVIDER VNX_T2_PROVIDER VNX_T3_PROVIDER 2>/dev/null || true
    unset VNX_T0_MODEL VNX_T1_MODEL VNX_T2_MODEL VNX_T3_MODEL 2>/dev/null || true
}

make_dispatch() {
    local content="$1"
    local tmp
    tmp="$(mktemp)"
    echo "$content" > "$tmp"
    echo "$tmp"
}

# ============================================================================
# Section 1: Mixed-Provider Scenario — Required Codex on Claude Terminal
# ============================================================================
echo ""
echo "=== Section 1: Mixed-provider — required codex_cli on claude_code ==="

_clean_env

# Create a realistic dispatch requiring codex_cli
DISPATCH=$(make_dispatch "$(cat <<'DISPATCH'
[[TARGET:B]]
Dispatch-ID: cert-mixed-provider-001
PR-ID: PR-4
Requires-Provider: codex_cli required
Requires-Model: sonnet
Role: backend-developer
Mode: normal

## Task
Certification: required codex_cli dispatch on claude_code terminal
DISPATCH
)")

# 1a. Extract provider metadata correctly
req_provider=$(vnx_dispatch_extract_requires_provider "$DISPATCH")
assert_eq "codex_cli" "$req_provider" "S1: provider extracted as codex_cli"

req_strength=$(vnx_dispatch_extract_requires_provider_strength "$DISPATCH")
assert_eq "required" "$req_strength" "S1: provider strength is required"

# 1b. Preflight detects the gap
event=$(vnx_check_provider_readiness "T2" "codex_cli" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S1: preflight blocks required codex_cli on claude_code T2"
assert_contains '"gap":"misconfigured"' "$event" "S1: gap classified as misconfigured"
assert_contains '"required_provider":"codex_cli"' "$event" "S1: requested provider in preflight event"
assert_contains '"actual_provider":"claude_code"' "$event" "S1: actual provider in preflight event"

# 1c. Enforcement blocks at dispatch time
event=$(vnx_eval_provider_routing "codex_cli" "required" "claude_code" "T2" "cert-mixed-provider-001") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S1: enforcement blocks dispatch"
assert_contains '"result":"mismatch_blocked"' "$event" "S1: enforcement produces mismatch_blocked"
assert_contains '"reason":"required provider mismatch"' "$event" "S1: explicit block reason in event"

# 1d. Evidence contains full routing identity
assert_contains '"requested_provider":"codex_cli"' "$event" "S1: evidence has requested_provider"
assert_contains '"actual_provider":"claude_code"' "$event" "S1: evidence has actual_provider"
assert_contains '"terminal":"T2"' "$event" "S1: evidence has terminal"
assert_contains '"dispatch":"cert-mixed-provider-001"' "$event" "S1: evidence has dispatch_id"

rm -f "$DISPATCH"

# ============================================================================
# Section 2: Mixed-Provider Scenario — Advisory Codex on Claude Terminal
# ============================================================================
echo ""
echo "=== Section 2: Mixed-provider — advisory codex_cli on claude_code ==="

_clean_env

DISPATCH=$(make_dispatch "$(cat <<'DISPATCH'
[[TARGET:A]]
Dispatch-ID: cert-mixed-provider-002
Requires-Provider: codex_cli
Role: backend-developer
DISPATCH
)")

# 2a. Advisory strength extracted
req_strength=$(vnx_dispatch_extract_requires_provider_strength "$DISPATCH")
assert_eq "advisory" "$req_strength" "S2: default strength is advisory"

# 2b. Enforcement warns but proceeds
event=$(vnx_eval_provider_routing "codex_cli" "advisory" "claude_code" "T1" "cert-mixed-provider-002")
rc=$?
assert_exit_code 0 "$rc" "S2: advisory mismatch proceeds (exit 0)"
assert_contains '"result":"mismatch_advisory"' "$event" "S2: mismatch_advisory in event"

# 2c. Evidence still records the mismatch (VR-6: advisory mismatches MUST be recorded)
assert_contains '"requested_provider":"codex_cli"' "$event" "S2: advisory mismatch records requested"
assert_contains '"actual_provider":"claude_code"' "$event" "S2: advisory mismatch records actual"

rm -f "$DISPATCH"

# ============================================================================
# Section 3: Mixed-Model Scenario — Required Opus on Sonnet-Pinned Terminal
# ============================================================================
echo ""
echo "=== Section 3: Mixed-model — required opus on sonnet-pinned (claude_code) ==="

_clean_env

DISPATCH=$(make_dispatch "$(cat <<'DISPATCH'
[[TARGET:A]]
Dispatch-ID: cert-mixed-model-001
Requires-Model: opus required
Role: architect
Mode: thinking
DISPATCH
)")

# 3a. Model metadata extracted
req_model=$(vnx_dispatch_extract_requires_model "$DISPATCH")
assert_eq "opus" "$req_model" "S3: model extracted as opus"

req_strength=$(vnx_dispatch_extract_requires_model_strength "$DISPATCH")
assert_eq "required" "$req_strength" "S3: model strength is required"

# 3b. Preflight: claude_code can switch → ready_with_switch
event=$(vnx_check_model_readiness "T1" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "S3: preflight passes (claude_code can switch)"
assert_contains '"result":"ready_with_switch"' "$event" "S3: preflight says ready_with_switch"

# 3c. Pre-switch evaluation says needs_switch
event=$(vnx_eval_model_routing "opus" "required" "claude_code" "T1" "cert-mixed-model-001")
rc=$?
assert_exit_code 0 "$rc" "S3: pre-switch eval returns needs_switch (exit 0)"
assert_contains '"result":"needs_switch"' "$event" "S3: eval says needs_switch"
assert_contains '"requested_model":"opus"' "$event" "S3: requested_model in pre-switch event"

# 3d. Simulate successful switch verification
pane_output="Model set to claude-opus-4-6 (default, 1M context)"
switch_result=$(vnx_verify_model_switch_output "$pane_output" "default")
assert_eq "switched" "$switch_result" "S3: pane output confirms switch to opus"

# 3e. Emit result → verified match, proceeds
event=$(vnx_emit_model_switch_result "opus" "switched" "claude-opus-4-6" "required" "T1" "cert-mixed-model-001")
rc=$?
assert_exit_code 0 "$rc" "S3: verified switch → dispatch proceeds"
assert_contains '"model_match":"verified_match"' "$event" "S3: verified_match in result"
assert_contains '"requested_model":"opus"' "$event" "S3: requested_model in result event"
assert_contains '"actual_model":"claude-opus-4-6"' "$event" "S3: actual_model in result event"
assert_contains '"switch_result":"switched"' "$event" "S3: switch_result in result event"

# 3f. Simulate FAILED switch → blocks required dispatch
event=$(vnx_emit_model_switch_result "opus" "unverified" "" "required" "T1" "cert-mixed-model-002") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S3: unverified switch on required → blocked"
assert_contains '"model_match":"mismatch_blocked"' "$event" "S3: unverified required → mismatch_blocked"

rm -f "$DISPATCH"

# ============================================================================
# Section 4: Mixed-Model Scenario — Required Opus on Gemini Terminal
# ============================================================================
echo ""
echo "=== Section 4: Mixed-model — required opus on gemini_cli terminal ==="

_clean_env
export VNX_T2_PROVIDER=gemini_cli

# 4a. Preflight: gemini cannot switch → not_ready
event=$(vnx_check_model_readiness "T2" "opus" "required") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S4: preflight blocks (gemini has no /model)"
assert_contains '"result":"not_ready"' "$event" "S4: preflight says not_ready"
assert_contains '"gap":"unsupported"' "$event" "S4: gap is unsupported"

# 4b. Pre-switch eval: unsupported → blocked for required
event=$(vnx_eval_model_routing "opus" "required" "gemini_cli" "T2" "cert-mixed-model-003") || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S4: eval blocks (unsupported required)"
assert_contains '"result":"unsupported"' "$event" "S4: eval says unsupported"
assert_contains '"reason"' "$event" "S4: eval provides reason"

# 4c. Advisory on gemini → proceeds with warning
event=$(vnx_eval_model_routing "opus" "advisory" "gemini_cli" "T2" "cert-mixed-model-004")
rc=$?
assert_exit_code 0 "$rc" "S4: advisory unsupported → proceeds"
assert_contains '"result":"unsupported"' "$event" "S4: advisory still says unsupported (logged)"

_clean_env

# ============================================================================
# Section 5: Full Dispatch Lifecycle — Metadata → Preflight → Enforcement
# ============================================================================
echo ""
echo "=== Section 5: Full dispatch lifecycle — end-to-end ==="

_clean_env

# Scenario: T3 runs Opus (default), dispatch requires Opus → everything passes
DISPATCH=$(make_dispatch "$(cat <<'DISPATCH'
[[TARGET:C]]
Dispatch-ID: cert-lifecycle-001
PR-ID: PR-4
Requires-Provider: claude_code required
Requires-Model: opus required
Role: quality-engineer
Mode: normal
ClearContext: true
DISPATCH
)")

# 5a. Extract all metadata
track=$(vnx_dispatch_extract_track "$DISPATCH")
assert_eq "C" "$track" "S5: track extracted as C"

provider=$(vnx_dispatch_extract_requires_provider "$DISPATCH")
assert_eq "claude_code" "$provider" "S5: provider is claude_code"

provider_str=$(vnx_dispatch_extract_requires_provider_strength "$DISPATCH")
assert_eq "required" "$provider_str" "S5: provider strength is required"

model=$(vnx_dispatch_extract_requires_model "$DISPATCH")
assert_eq "opus" "$model" "S5: model is opus"

model_str=$(vnx_dispatch_extract_requires_model_strength "$DISPATCH")
assert_eq "required" "$model_str" "S5: model strength is required"

# 5b. Preflight: T3 default env → claude_code + default(opus) → ready
events=$(vnx_preflight_routing_readiness "T3" "claude_code" "required" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "S5: full preflight passes for T3 with default env"

# 5c. Provider enforcement: claude_code required on claude_code T3 → match
event=$(vnx_eval_provider_routing "claude_code" "required" "claude_code" "T3" "cert-lifecycle-001")
rc=$?
assert_exit_code 0 "$rc" "S5: provider enforcement passes"
assert_contains '"result":"match"' "$event" "S5: provider match confirmed"

# 5d. Model pre-check: opus on claude_code → needs_switch
event=$(vnx_eval_model_routing "opus" "required" "claude_code" "T3" "cert-lifecycle-001")
rc=$?
assert_exit_code 0 "$rc" "S5: model pre-check says needs_switch"

# 5e. Simulate verified switch
switch_result=$(vnx_verify_model_switch_output "Switched to claude-opus-4-6" "default")
assert_eq "switched" "$switch_result" "S5: switch verified"

event=$(vnx_emit_model_switch_result "opus" "switched" "claude-opus-4-6" "required" "T3" "cert-lifecycle-001")
rc=$?
assert_exit_code 0 "$rc" "S5: model switch result → proceed"
assert_contains '"model_match":"verified_match"' "$event" "S5: verified_match in lifecycle"

rm -f "$DISPATCH"

# ============================================================================
# Section 6: Evidence Completeness — All Events Contain Identity Fields
# ============================================================================
echo ""
echo "=== Section 6: Evidence completeness ==="

_clean_env

# VR-3: Every dispatch delivery MUST record actual_provider and actual_model
# VR-4: unsupported/failed MUST be recorded as coordination events
# VR-6: Advisory mismatches MUST be recorded

# 6a. Provider match event has all identity fields
event=$(vnx_eval_provider_routing "claude_code" "required" "claude_code" "T1" "cert-evidence-001")
assert_contains '"event":"provider_routing"' "$event" "S6: event type present"
assert_contains '"result":"match"' "$event" "S6: result field present"
assert_contains '"provider":"claude_code"' "$event" "S6: provider field present"
assert_contains '"terminal":"T1"' "$event" "S6: terminal field present"
assert_contains '"dispatch":"cert-evidence-001"' "$event" "S6: dispatch field present"

# 6b. Model switch result event has requested + actual + switch_result
event=$(vnx_emit_model_switch_result "opus" "switched" "claude-opus-4-6" "required" "T1" "cert-evidence-002")
assert_contains '"requested_model":"opus"' "$event" "S6: requested_model present"
assert_contains '"actual_model":"claude-opus-4-6"' "$event" "S6: actual_model present"
assert_contains '"switch_result":"switched"' "$event" "S6: switch_result present"
assert_contains '"model_match":"verified_match"' "$event" "S6: model_match present"
assert_contains '"strength":"required"' "$event" "S6: strength present"

# 6c. Blocked events have explicit reason
event=$(vnx_eval_provider_routing "codex_cli" "required" "claude_code" "T2" "cert-evidence-003") || true
assert_contains '"reason"' "$event" "S6: blocked event has reason field"

# 6d. Unsupported events have reason (VR-4)
event=$(vnx_eval_model_routing "opus" "required" "gemini_cli" "T2" "cert-evidence-004") || true
assert_contains '"reason"' "$event" "S6: unsupported event has reason (VR-4)"

# ============================================================================
# Section 7: Pinned Terminal Assumptions — Explicit Check Before Chain
# ============================================================================
echo ""
echo "=== Section 7: Pinned terminal assumptions ==="

_clean_env

# 7a. Default chain: T0=Opus, T1=Sonnet, T2=Sonnet, T3=Opus
events=$(vnx_check_pinned_assumptions)
rc=$?
assert_exit_code 0 "$rc" "S7: default chain assumptions verified"

# Count all 4 terminals verified
verified_count=$(echo "$events" | grep -c '"result":"verified"' || true)
assert_eq "4" "$verified_count" "S7: all 4 terminals verified before chain start"

# 7b. Verify specific terminal models match contract
t0_model=$(vnx_resolve_terminal_model "T0")
assert_eq "default" "$t0_model" "S7: T0 model = default (Opus)"

t1_model=$(vnx_resolve_terminal_model "T1")
assert_eq "sonnet" "$t1_model" "S7: T1 model = sonnet"

t2_model=$(vnx_resolve_terminal_model "T2")
assert_eq "sonnet" "$t2_model" "S7: T2 model = sonnet"

t3_model=$(vnx_resolve_terminal_model "T3")
assert_eq "default" "$t3_model" "S7: T3 model = default (Opus)"

# 7c. Drift detection when operator changes terminal
export VNX_T1_PROVIDER=codex_cli
export VNX_T2_MODEL=opus
events=$(vnx_check_pinned_assumptions) || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S7: drift detected after terminal changes"
drift_count=$(echo "$events" | grep -c '"result":"drift"' || true)
assert_eq "2" "$drift_count" "S7: exactly 2 terminals show drift (T1 provider, T2 model)"

_clean_env

# ============================================================================
# Section 8: Cross-Layer Consistency — Shell and Python Agree
# ============================================================================
echo ""
echo "=== Section 8: Cross-layer consistency ==="

_clean_env

# 8a. Shell says T1 claude_code required on default env → ready
event=$(vnx_check_provider_readiness "T1" "claude_code" "required")
rc=$?
assert_exit_code 0 "$rc" "S8: shell says T1 claude_code ready"

# 8b. Python says the same
py_result=$(python3 -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')
import routing_preflight as rp
# Clean env
for t in ('T0','T1','T2','T3'):
    os.environ.pop(f'VNX_{t}_PROVIDER', None)
    os.environ.pop(f'VNX_{t}_MODEL', None)
r = rp.check_provider_readiness('T1', 'claude_code', 'required')
print('ready' if r.ready else 'not_ready')
")
assert_eq "ready" "$py_result" "S8: Python agrees T1 claude_code ready"

# 8c. Shell says T1 codex_cli required → not ready
vnx_check_provider_readiness "T1" "codex_cli" "required" >/dev/null 2>&1 || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S8: shell says T1 codex_cli not ready"

# 8d. Python agrees
py_result=$(python3 -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')
import routing_preflight as rp
for t in ('T0','T1','T2','T3'):
    os.environ.pop(f'VNX_{t}_PROVIDER', None)
    os.environ.pop(f'VNX_{t}_MODEL', None)
r = rp.check_provider_readiness('T1', 'codex_cli', 'required')
print('not_ready' if not r.ready else 'ready')
")
assert_eq "not_ready" "$py_result" "S8: Python agrees T1 codex_cli not ready"

# 8e. Model readiness cross-check: opus on sonnet-pinned T1 (switch capable)
event=$(vnx_check_model_readiness "T1" "opus" "required")
rc=$?
assert_exit_code 0 "$rc" "S8: shell says T1 opus ready (switch capable)"

py_result=$(python3 -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')
import routing_preflight as rp
for t in ('T0','T1','T2','T3'):
    os.environ.pop(f'VNX_{t}_PROVIDER', None)
    os.environ.pop(f'VNX_{t}_MODEL', None)
r = rp.check_model_readiness('T1', 'opus', 'required')
print('ready' if r.ready else 'not_ready')
print('switch' if r.can_switch else 'no_switch')
")
assert_contains "ready" "$py_result" "S8: Python agrees T1 opus ready"
assert_contains "switch" "$py_result" "S8: Python agrees can_switch=True"

# 8f. Gemini model check cross-layer
export VNX_T2_PROVIDER=gemini_cli
vnx_check_model_readiness "T2" "opus" "required" >/dev/null 2>&1 || rc=$?
rc=${rc:-0}
assert_exit_code 1 "$rc" "S8: shell says gemini T2 opus not ready"

py_result=$(python3 -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')
os.environ['VNX_T2_PROVIDER'] = 'gemini_cli'
import routing_preflight as rp
r = rp.check_model_readiness('T2', 'opus', 'required')
print('not_ready' if not r.ready else 'ready')
print(r.gap)
")
assert_contains "not_ready" "$py_result" "S8: Python agrees gemini T2 opus not ready"
assert_contains "unsupported" "$py_result" "S8: Python agrees gap=unsupported"

_clean_env

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "=== Results ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo "RESULT: FAIL ($FAIL test(s) failed)"
    exit 1
else
    echo "RESULT: PASS — all $PASS routing certification tests passed"
    exit 0
fi
