#!/usr/bin/env bash
# Regression test: OI-1320 — role-alias mapping must propagate into gather_intelligence.py
#
# Finding (Codex, PR #317):
#   _validate_agent_intelligence maps legacy aliases (developer → backend-developer)
#   via map_role_to_skill, but gather_dispatch_intelligence was called with the
#   raw alias. gather_intelligence.py gather returns rc≠0 (dispatch_blocked=true)
#   for an unregistered alias, which the caller classified as [DEPENDENCY_ERROR].
#
# Fix (scripts/dispatcher_v8_minimal.sh):
#   - _validate_agent_intelligence sets _PD_MAPPED_ROLE="$_mapped_role" on success.
#   - process_dispatches() calls gather_dispatch_intelligence with
#     "${_PD_MAPPED_ROLE:-$agent_role}" instead of "$agent_role".

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCHER="$SCRIPT_DIR/../scripts/dispatcher_v8_minimal.sh"
DISPATCH_CREATE="$PROJECT_ROOT/scripts/lib/dispatch_create.sh"

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# Premise: gather_intelligence.py gather with unmapped alias → non-zero exit
# (confirms the bug is real and this test would have caught it).
# ---------------------------------------------------------------------------
set +e
alias_out=$(python3 "$PROJECT_ROOT/scripts/gather_intelligence.py" gather "test task" "T1" "developer" 2>&1)
alias_rc=$?
canonical_out=$(python3 "$PROJECT_ROOT/scripts/gather_intelligence.py" gather "test task" "T1" "backend-developer" 2>&1)
canonical_rc=$?
set -e

if [ "$alias_rc" -ne 0 ]; then
    pass "gather_intelligence.py gather with raw alias 'developer' returns rc≠0 (bug premise confirmed)"
else
    fail "expected non-zero rc for raw alias 'developer'; got rc=$alias_rc — premise has shifted"
fi

if echo "$alias_out" | grep -q '"dispatch_blocked".*true\|"dispatch_blocked": true'; then
    pass "gather_intelligence.py gather with 'developer' returns dispatch_blocked=true"
else
    fail "expected dispatch_blocked=true for raw alias; output was: $alias_out"
fi

if [ "$canonical_rc" -eq 0 ]; then
    pass "gather_intelligence.py gather with canonical 'backend-developer' returns rc=0"
else
    fail "expected rc=0 for canonical 'backend-developer'; got rc=$canonical_rc"
fi

# ---------------------------------------------------------------------------
# Build a harness that sources _validate_agent_intelligence from the dispatcher
# and verifies _PD_MAPPED_ROLE is set to the canonical name after a successful call.
# ---------------------------------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Extract _validate_agent_intelligence function body
awk '
    /^_validate_agent_intelligence\(\)/ { capture = 1 }
    capture { print }
    capture && /^}$/ { exit }
' "$DISPATCHER" > "$TMP/func.sh"

if [ ! -s "$TMP/func.sh" ]; then
    fail "could not extract _validate_agent_intelligence from dispatcher"
    echo "Results: $PASS passed, $FAIL failed"
    exit 1
fi

HARNESS="$TMP/harness.sh"
cat > "$HARNESS" <<HARNESS_EOF
#!/usr/bin/env bash
set -uo pipefail

VNX_DIR="$PROJECT_ROOT"
_PD_MAPPED_ROLE=""

log() { :; }
log_structured_failure() { :; }

set +e
source "$DISPATCH_CREATE" 2>/dev/null
set -e

if ! command -v map_role_to_skill >/dev/null 2>&1; then
    map_role_to_skill() {
        case "\$1" in
            developer) echo "backend-developer" ;;
            *) echo "\$1" ;;
        esac
    }
fi

source "$TMP/func.sh"

_rc=0
if _validate_agent_intelligence "\$2" "\$1"; then
    _rc=0
else
    _rc=\$?
fi
echo "EXIT_RC=\$_rc"
echo "MAPPED_ROLE=\$_PD_MAPPED_ROLE"
HARNESS_EOF
chmod +x "$HARNESS"

# ---------------------------------------------------------------------------
# Test: legacy alias 'developer' → _PD_MAPPED_ROLE must be set to 'backend-developer'
# ---------------------------------------------------------------------------
DISPATCH1="$TMP/dispatch_alias.md"
cat > "$DISPATCH1" <<'EOF'
# Dispatch
Role: developer
Track: A
EOF

set +e
out1=$(bash "$HARNESS" "developer" "$DISPATCH1" 2>&1)
set -e
rc1=$(echo "$out1" | grep -oE 'EXIT_RC=[0-9]+' | tail -1 | cut -d= -f2)
mapped1=$(echo "$out1" | grep 'MAPPED_ROLE=' | tail -1 | cut -d= -f2)

if [ "${rc1:-1}" = "0" ]; then
    pass "_validate_agent_intelligence returns 0 for legacy alias 'developer'"
else
    fail "_validate_agent_intelligence rejected 'developer'; rc=$rc1 output=$out1"
fi

if [ "${mapped1:-}" = "backend-developer" ]; then
    pass "_PD_MAPPED_ROLE set to 'backend-developer' after validating alias (fix verified)"
else
    fail "_PD_MAPPED_ROLE not set correctly; got='${mapped1:-}' (expected 'backend-developer')"
fi

if ! grep -q "\[DEPENDENCY_ERROR\]" "$DISPATCH1"; then
    pass "no [DEPENDENCY_ERROR] marker in dispatch after validating 'developer'"
else
    fail "regression: [DEPENDENCY_ERROR] added for valid legacy alias 'developer'"
    sed 's/^/      /' "$DISPATCH1"
fi

# ---------------------------------------------------------------------------
# Test: native canonical name 'backend-developer' → _PD_MAPPED_ROLE also set
# ---------------------------------------------------------------------------
DISPATCH2="$TMP/dispatch_canonical.md"
cat > "$DISPATCH2" <<'EOF'
# Dispatch
Role: backend-developer
Track: A
EOF

set +e
out2=$(bash "$HARNESS" "backend-developer" "$DISPATCH2" 2>&1)
set -e
rc2=$(echo "$out2" | grep -oE 'EXIT_RC=[0-9]+' | tail -1 | cut -d= -f2)
mapped2=$(echo "$out2" | grep 'MAPPED_ROLE=' | tail -1 | cut -d= -f2)

if [ "${rc2:-1}" = "0" ]; then
    pass "_validate_agent_intelligence returns 0 for canonical 'backend-developer'"
else
    fail "_validate_agent_intelligence rejected 'backend-developer'; rc=$rc2 output=$out2"
fi

if [ "${mapped2:-}" = "backend-developer" ]; then
    pass "_PD_MAPPED_ROLE set to 'backend-developer' for canonical input"
else
    fail "_PD_MAPPED_ROLE not set for canonical input; got='${mapped2:-}'"
fi

# ---------------------------------------------------------------------------
# Verify the call-site fix: confirm the dispatcher source uses _PD_MAPPED_ROLE
# in the gather_dispatch_intelligence invocation.
# ---------------------------------------------------------------------------
if grep -q '_PD_MAPPED_ROLE' "$DISPATCHER"; then
    pass "_PD_MAPPED_ROLE global declared in dispatcher"
else
    fail "_PD_MAPPED_ROLE global not found in dispatcher"
fi

if grep -qE 'gather_dispatch_intelligence.*\$\{_PD_MAPPED_ROLE' "$DISPATCHER"; then
    pass "gather_dispatch_intelligence call uses \${_PD_MAPPED_ROLE:-...} (fix present)"
else
    fail "gather_dispatch_intelligence call does not reference _PD_MAPPED_ROLE — fix missing"
fi

# ---------------------------------------------------------------------------
# Regression: run the existing alias validation test
# ---------------------------------------------------------------------------
echo ""
echo "=== Running existing regression: test_validate_agent_intelligence_alias.sh ==="
set +e
bash "$SCRIPT_DIR/test_validate_agent_intelligence_alias.sh"
alias_test_rc=$?
set -e

if [ "$alias_test_rc" -eq 0 ]; then
    pass "existing alias validation test (test_validate_agent_intelligence_alias.sh) still passes"
else
    fail "regression in test_validate_agent_intelligence_alias.sh (rc=$alias_test_rc)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
