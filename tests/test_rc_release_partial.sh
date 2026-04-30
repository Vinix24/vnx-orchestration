#!/usr/bin/env bash
# CFX-16: rc_release_on_failure emits lease_released_on_failure_partial to dispatch_register
# when failure_recorded=false, lease_released=true, cleanup_complete=false.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFECYCLE="$SCRIPT_DIR/../scripts/lib/dispatch_lifecycle.sh"

PASS=0
FAIL=0

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label — expected '$needle' not found in output: ${haystack:-<empty>}"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        echo "FAIL: $label — unexpected '$needle' found in output: $haystack"
        FAIL=$((FAIL + 1))
    else
        echo "PASS: $label"
        PASS=$((PASS + 1))
    fi
}

# Run rc_release_on_failure with mocked dependencies.
# Captures stdout from the mock dispatch_register.py (prints appended event names).
_run_scenario() {
    local mock_json="$1"
    local TMP
    TMP="$(mktemp -d)"

    mkdir -p "$TMP/scripts/lib"
    printf '%s\n' \
        'import sys' \
        'if len(sys.argv) >= 3 and sys.argv[1] == "append":' \
        '    print(sys.argv[2])' \
        > "$TMP/scripts/lib/dispatch_register.py"
    printf '%s' "$mock_json" > "$TMP/mock.json"

    local SCRIPT="$TMP/run.sh"
    cat > "$SCRIPT" <<HEREDOC
#!/bin/bash
set -uo pipefail
log() { :; }
log_structured_failure() { :; }
emit_lease_cleanup_audit() { :; }
rc_release_lease() { :; }
_rc_enabled() { return 0; }
_call_cleanup_worker_exit() { :; }
_rc_python() { cat "$TMP/mock.json"; return 0; }
VNX_DIR="$TMP"
eval "\$(awk '
    /^rc_release_on_failure\(\)/ { inside=1 }
    inside {
        print
        n += gsub(/[{]/, "&")
        n -= gsub(/[}]/, "&")
        if (started && n==0) exit
        if (n>0) started=1
    }
' "$LIFECYCLE")"
rc_release_on_failure "dispatch-001" "attempt-1" "T1" "5" "test"
HEREDOC

    bash "$SCRIPT" 2>/dev/null
    rm -rf "$TMP"
}

# --- Scenario 1: partial cleanup (failure_recorded=false, lease_released=true, cleanup_complete=false) ---
out=$(_run_scenario '{"failure_recorded": false, "lease_released": true, "cleanup_complete": false, "lease_error": null}')
assert_contains "partial: register emits lease_released_on_failure_partial" \
    "lease_released_on_failure_partial" "$out"

# --- Scenario 2: full success (failure_recorded=true, lease_released=true, cleanup_complete=true) ---
out=$(_run_scenario '{"failure_recorded": true, "lease_released": true, "cleanup_complete": true, "lease_error": null}')
assert_not_contains "full success: no partial register event" \
    "lease_released_on_failure_partial" "$out"

# --- Scenario 3: lease release failed (lease_released=false) ---
out=$(_run_scenario '{"failure_recorded": false, "lease_released": false, "cleanup_complete": false, "lease_error": "stale gen"}')
assert_not_contains "lease release failure: no partial register event" \
    "lease_released_on_failure_partial" "$out"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
