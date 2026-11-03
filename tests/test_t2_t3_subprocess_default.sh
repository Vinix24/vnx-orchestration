#!/usr/bin/env bash
# Tests for the T1-T3 adapter rule (F32 Wave D PR-1, T-G1).
# T-G1 removed the worker selector: T1/T2/T3 are always subprocess whatever
# VNX_ADAPTER_Tx says. The contract test for the real function is
# tests/test_dispatch_deliver_contract.py.

set -euo pipefail

PASS=0
FAIL=0

# Mirror of the dispatch_deliver.sh rule (T-G1); keep in step with that function.
resolve_adapter() {
    local terminal_id="$1"
    if [[ "$terminal_id" =~ ^T[123]$ ]]; then
        echo "subprocess"
        return
    fi
    local adapter_var="VNX_ADAPTER_${terminal_id}"
    echo "${!adapter_var:-tmux}"
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label — expected='$expected' got='$actual'"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== T2/T3 subprocess-default adapter selection tests ==="

# --- T0: defaults to tmux ---
unset VNX_ADAPTER_T0
assert_eq "T0 unset -> tmux" "tmux" "$(resolve_adapter T0)"

VNX_ADAPTER_T0=subprocess
assert_eq "T0 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T0)"
unset VNX_ADAPTER_T0

VNX_ADAPTER_T0=tmux
assert_eq "T0 explicit tmux -> tmux" "tmux" "$(resolve_adapter T0)"
unset VNX_ADAPTER_T0

# --- T1: defaults to subprocess ---
unset VNX_ADAPTER_T1
assert_eq "T1 unset -> subprocess" "subprocess" "$(resolve_adapter T1)"

VNX_ADAPTER_T1=tmux
assert_eq "T1 ignores the tmux value -> subprocess" "subprocess" "$(resolve_adapter T1)"
unset VNX_ADAPTER_T1

VNX_ADAPTER_T1=subprocess
assert_eq "T1 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T1)"
unset VNX_ADAPTER_T1

# --- T2: defaults to subprocess (new in Wave D PR-1) ---
unset VNX_ADAPTER_T2
assert_eq "T2 unset -> subprocess" "subprocess" "$(resolve_adapter T2)"

VNX_ADAPTER_T2=tmux
assert_eq "T2 ignores the tmux value -> subprocess" "subprocess" "$(resolve_adapter T2)"
unset VNX_ADAPTER_T2

VNX_ADAPTER_T2=subprocess
assert_eq "T2 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T2)"
unset VNX_ADAPTER_T2

# --- T3: defaults to subprocess (new in Wave D PR-1) ---
unset VNX_ADAPTER_T3
assert_eq "T3 unset -> subprocess" "subprocess" "$(resolve_adapter T3)"

VNX_ADAPTER_T3=tmux
assert_eq "T3 ignores the tmux value -> subprocess" "subprocess" "$(resolve_adapter T3)"
unset VNX_ADAPTER_T3

VNX_ADAPTER_T3=subprocess
assert_eq "T3 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T3)"
unset VNX_ADAPTER_T3

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
