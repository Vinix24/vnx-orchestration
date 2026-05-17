#!/usr/bin/env bash
set -euo pipefail

# Tests for install-central.sh --dry-run mode.
# Verifies: no filesystem mutations, expected output lines, exit code 0, idempotency.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/install-central.sh"
TMP_TARGET="/tmp/vnx-central-test-$$"

PASS=0
FAIL=0

pass() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [x] $1" >&2; FAIL=$((FAIL + 1)); }

# Clean up tmp target if created during tests
cleanup() { rm -rf "$TMP_TARGET" 2>/dev/null || true; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Test 1: --dry-run exits 0 and produces expected output
# ---------------------------------------------------------------------------
echo "Test 1: --dry-run exits 0 with expected output"

output="$(bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version v1.0.0-rc2 2>&1)"
exit_code=$?

if [ "$exit_code" -eq 0 ]; then
  pass "exit code 0"
else
  fail "exit code was ${exit_code}, expected 0"
fi

for expected in \
  "VNX Central Install" \
  "version" \
  "target" \
  "DRY RUN" \
  "Prerequisites OK" \
  "Symlink updated" \
  "Shim installed" \
  "Verification complete" \
  "installed successfully"
do
  if echo "$output" | grep -q "$expected"; then
    pass "output contains: ${expected}"
  else
    fail "output missing: ${expected}"
  fi
done

# ---------------------------------------------------------------------------
# Test 2: --dry-run does NOT create filesystem artifacts
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: --dry-run leaves no filesystem artifacts"

bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version v1.0.0-rc2 >/dev/null 2>&1

if [ ! -e "$TMP_TARGET" ]; then
  pass "target dir not created"
else
  fail "target dir was created (should not exist in dry-run): ${TMP_TARGET}"
fi

# ---------------------------------------------------------------------------
# Test 3: --dry-run output contains git clone step (not actual clone)
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: --dry-run shows git clone without running it"

output="$(bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version v1.0.0-rc2 2>&1)"

if echo "$output" | grep -q "git clone"; then
  pass "dry-run output shows git clone step"
else
  fail "dry-run output missing git clone step"
fi

if [ ! -d "${TMP_TARGET}/versions" ]; then
  pass "versions/ dir not created in dry-run"
else
  fail "versions/ dir was created (should not exist in dry-run)"
fi

# ---------------------------------------------------------------------------
# Test 4: --help exits 0 and shows usage
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: --help exits 0 with usage"

help_output="$(bash "$SCRIPT" --help 2>&1)"
help_code=$?

if [ "$help_code" -eq 0 ]; then
  pass "--help exit code 0"
else
  fail "--help exit code was ${help_code}"
fi

for expected in "--target" "--version" "--source" "--dry-run"; do
  if echo "$help_output" | grep -qF -- "$expected"; then
    pass "--help shows: ${expected}"
  else
    fail "--help missing: ${expected}"
  fi
done

# ---------------------------------------------------------------------------
# Test 5: bash -n syntax check
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: syntax check install-central.sh"

if bash -n "$SCRIPT"; then
  pass "install-central.sh passes bash -n"
else
  fail "install-central.sh fails bash -n"
fi

# ---------------------------------------------------------------------------
# Test 6: Re-run with same args (idempotency) in dry-run = same exit code
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: idempotent re-run (dry-run)"

run1="$(bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version v1.0.0-rc2; echo $?)"
run2="$(bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version v1.0.0-rc2; echo $?)"

# Both should exit 0 (captured as last line)
code1=$(echo "$run1" | tail -1)
code2=$(echo "$run2" | tail -1)

if [ "$code1" -eq 0 ] && [ "$code2" -eq 0 ]; then
  pass "both runs exit 0 (idempotent)"
else
  fail "idempotency broken: run1=${code1} run2=${code2}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
echo ""

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
