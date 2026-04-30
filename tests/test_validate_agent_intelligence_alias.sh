#!/usr/bin/env bash
# Regression test: _validate_agent_intelligence must accept legacy role
# aliases by mapping them through map_role_to_skill BEFORE invoking
# gather_intelligence.py validate.
#
# Finding (Codex round-2, PR #317):
#   _validate_stuck_files maps aliases (developer → backend-developer) via
#   map_role_to_skill, but _validate_agent_intelligence called
#   gather_intelligence.py validate with the raw role string. That CLI
#   returns EXIT_VALIDATION (rc=10) for unmapped aliases, and the function
#   treated any non-zero exit as [DEPENDENCY_ERROR]. Result: every legacy
#   alias was blocked as a runtime dependency failure instead of dispatching.
#
# Fix (scripts/dispatcher_v8_minimal.sh):
#   - Map role through map_role_to_skill before validate call.
#   - Distinguish rc=10 (registry miss → SKILL_INVALID) from other non-zero
#     codes (genuine runtime failure → DEPENDENCY_ERROR).
#
# This test extracts _validate_agent_intelligence from the dispatcher,
# stubs its logging dependencies, sources map_role_to_skill from
# dispatch_create.sh, and exercises three scenarios.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCHER="$PROJECT_ROOT/scripts/dispatcher_v8_minimal.sh"
DISPATCH_CREATE="$PROJECT_ROOT/scripts/lib/dispatch_create.sh"

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# Sanity: gather_intelligence.py must return rc=10 for the raw alias and
# rc=0 for the mapped name. This pins the contract the fix relies on.
# ---------------------------------------------------------------------------
set +e
python3 "$PROJECT_ROOT/scripts/gather_intelligence.py" validate developer >/dev/null 2>&1
raw_rc=$?
python3 "$PROJECT_ROOT/scripts/gather_intelligence.py" validate backend-developer >/dev/null 2>&1
mapped_rc=$?
set -e

if [ "$raw_rc" -eq 10 ]; then
    pass "gather_intelligence rejects raw alias 'developer' with rc=10 (EXIT_VALIDATION)"
else
    fail "expected rc=10 for raw alias 'developer'; got rc=$raw_rc — finding premise has shifted"
fi

if [ "$mapped_rc" -eq 0 ]; then
    pass "gather_intelligence accepts mapped name 'backend-developer' with rc=0"
else
    fail "expected rc=0 for 'backend-developer'; got rc=$mapped_rc — registry drift"
fi

# ---------------------------------------------------------------------------
# Build a self-contained harness around _validate_agent_intelligence.
# We extract just the function body from the dispatcher to avoid running
# its full boot sequence (singleton_enforcer, broker init, etc).
# ---------------------------------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

HARNESS="$TMP/harness.sh"

# Extract the function from the dispatcher (start at the function header,
# stop at the next blank-line-then-comment block boundary).
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

cat > "$HARNESS" <<HARNESS_EOF
#!/usr/bin/env bash
set -uo pipefail

# Required globals/stubs the function references.
VNX_DIR="$PROJECT_ROOT"

log() { :; }
log_structured_failure() { :; }

# map_role_to_skill is sourced from dispatch_create.sh. The library has
# its own dependencies, so source defensively under set +e.
set +e
source "$DISPATCH_CREATE" 2>/dev/null
set -e

if ! command -v map_role_to_skill >/dev/null 2>&1; then
    # Fallback: inline the alias map for the cases this test cares about.
    map_role_to_skill() {
        case "\$1" in
            developer) echo "backend-developer" ;;
            *) echo "\$1" ;;
        esac
    }
fi

source "$TMP/func.sh"

# Argument 1 = role to validate, argument 2 = dispatch fixture path.
# The function body internally re-enables set -e after a transient set +e,
# so a plain invocation under errexit aborts the shell on return 1 before
# we can capture rc. Use the "if" form, which suspends errexit for the
# tested command and lets us read the exit status directly.
_rc=0
if _validate_agent_intelligence "\$2" "\$1"; then
    _rc=0
else
    _rc=\$?
fi
echo "EXIT_RC=\$_rc"
HARNESS_EOF

chmod +x "$HARNESS"

# ---------------------------------------------------------------------------
# Test 1: legacy alias 'developer' must be accepted (mapped → backend-developer).
# Should return 0, must NOT add [DEPENDENCY_ERROR] or [SKILL_INVALID] to dispatch.
# ---------------------------------------------------------------------------
DISPATCH1="$TMP/dispatch_alias.md"
cat > "$DISPATCH1" <<'EOF'
# Dispatch
Role: developer
EOF

set +e
out1=$(bash "$HARNESS" "developer" "$DISPATCH1" 2>&1)
set -e
rc1=$(echo "$out1" | grep -oE 'EXIT_RC=[0-9]+' | tail -1 | cut -d= -f2)

if [ "${rc1:-1}" = "0" ]; then
    pass "_validate_agent_intelligence accepts legacy alias 'developer' (rc=0)"
else
    fail "_validate_agent_intelligence rejected 'developer'; rc=$rc1 output=$out1"
fi

if ! grep -q "\[DEPENDENCY_ERROR\]" "$DISPATCH1"; then
    pass "no spurious [DEPENDENCY_ERROR] marker added for valid legacy alias"
else
    fail "regression: [DEPENDENCY_ERROR] added for valid alias 'developer'"
    echo "    dispatch contents:"
    sed 's/^/      /' "$DISPATCH1"
fi

if ! grep -q "\[SKILL_INVALID\]" "$DISPATCH1"; then
    pass "no spurious [SKILL_INVALID] marker added for valid legacy alias"
else
    fail "regression: [SKILL_INVALID] added for valid alias 'developer'"
fi

# ---------------------------------------------------------------------------
# Test 2: native skill name 'backend-developer' continues to validate.
# ---------------------------------------------------------------------------
DISPATCH2="$TMP/dispatch_native.md"
cat > "$DISPATCH2" <<'EOF'
# Dispatch
Role: backend-developer
EOF

set +e
out2=$(bash "$HARNESS" "backend-developer" "$DISPATCH2" 2>&1)
set -e
rc2=$(echo "$out2" | grep -oE 'EXIT_RC=[0-9]+' | tail -1 | cut -d= -f2)

if [ "${rc2:-1}" = "0" ]; then
    pass "_validate_agent_intelligence accepts native skill 'backend-developer' (rc=0)"
else
    fail "_validate_agent_intelligence rejected 'backend-developer'; rc=$rc2 output=$out2"
fi

if ! grep -qE "\[DEPENDENCY_ERROR\]|\[SKILL_INVALID\]" "$DISPATCH2"; then
    pass "no error markers added for valid native skill"
else
    fail "regression: error marker added for valid native skill"
fi

# ---------------------------------------------------------------------------
# Test 3: genuinely invalid role must be rejected with [SKILL_INVALID]
# (NOT [DEPENDENCY_ERROR]). gather_intelligence returns rc=10, our fix
# routes that to the SKILL_INVALID branch.
# ---------------------------------------------------------------------------
DISPATCH3="$TMP/dispatch_bogus.md"
cat > "$DISPATCH3" <<'EOF'
# Dispatch
Role: not-a-real-skill-xyz
EOF

set +e
out3=$(bash "$HARNESS" "not-a-real-skill-xyz" "$DISPATCH3" 2>&1)
set -e
rc3=$(echo "$out3" | grep -oE 'EXIT_RC=[0-9]+' | tail -1 | cut -d= -f2)

if [ "${rc3:-0}" = "1" ]; then
    pass "_validate_agent_intelligence rejects unknown skill (rc=1)"
else
    fail "_validate_agent_intelligence should reject unknown skill; rc=$rc3 output=$out3"
fi

if grep -q "\[SKILL_INVALID\]" "$DISPATCH3"; then
    pass "[SKILL_INVALID] marker added for unknown skill (correct category)"
else
    fail "expected [SKILL_INVALID] for unknown skill; dispatch contents:"
    sed 's/^/      /' "$DISPATCH3"
fi

if ! grep -q "\[DEPENDENCY_ERROR\]" "$DISPATCH3"; then
    pass "no [DEPENDENCY_ERROR] miscategorisation for unknown skill (rc=10 routed correctly)"
else
    fail "regression: rc=10 was miscategorised as [DEPENDENCY_ERROR]"
    echo "    dispatch contents:"
    sed 's/^/      /' "$DISPATCH3"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
