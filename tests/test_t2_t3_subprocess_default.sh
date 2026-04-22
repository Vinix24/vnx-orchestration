#!/usr/bin/env bash
# Tests for T2/T3 subprocess-default adapter selection logic (F32 Wave D PR-1)
# Verifies that T1/T2/T3 default to subprocess and T0 defaults to tmux,
# and that VNX_ADAPTER_Tx=tmux opts out correctly.

set -euo pipefail

PASS=0
FAIL=0

# Inline the adapter selection logic from dispatch_deliver.sh lines 516-520
resolve_adapter() {
    local terminal_id="$1"
    local adapter_var="VNX_ADAPTER_${terminal_id}"
    local adapter_type="${!adapter_var:-tmux}"
    if [[ "$terminal_id" =~ ^T[123]$ && "$adapter_type" == "tmux" && -z "${!adapter_var:-}" ]]; then
        adapter_type="subprocess"
    fi
    echo "$adapter_type"
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
assert_eq "T1 explicit tmux opt-out -> tmux" "tmux" "$(resolve_adapter T1)"
unset VNX_ADAPTER_T1

VNX_ADAPTER_T1=subprocess
assert_eq "T1 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T1)"
unset VNX_ADAPTER_T1

# --- T2: defaults to subprocess (new in Wave D PR-1) ---
unset VNX_ADAPTER_T2
assert_eq "T2 unset -> subprocess" "subprocess" "$(resolve_adapter T2)"

VNX_ADAPTER_T2=tmux
assert_eq "T2 explicit tmux opt-out -> tmux" "tmux" "$(resolve_adapter T2)"
unset VNX_ADAPTER_T2

VNX_ADAPTER_T2=subprocess
assert_eq "T2 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T2)"
unset VNX_ADAPTER_T2

# --- T3: defaults to subprocess (new in Wave D PR-1) ---
unset VNX_ADAPTER_T3
assert_eq "T3 unset -> subprocess" "subprocess" "$(resolve_adapter T3)"

VNX_ADAPTER_T3=tmux
assert_eq "T3 explicit tmux opt-out -> tmux" "tmux" "$(resolve_adapter T3)"
unset VNX_ADAPTER_T3

VNX_ADAPTER_T3=subprocess
assert_eq "T3 explicit subprocess -> subprocess" "subprocess" "$(resolve_adapter T3)"
unset VNX_ADAPTER_T3

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
