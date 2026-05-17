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
# Test 7: invalid --version (path-traversal) exits EX_CONFIG (78)
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: invalid --version exits 78 (EX_CONFIG)"

bad_version_code=0
bad_version_output="$(bash "$SCRIPT" --dry-run --target "$TMP_TARGET" --version "../etc/passwd" 2>&1)" || bad_version_code=$?

if [ "$bad_version_code" -eq 78 ]; then
  pass "invalid --version exits 78"
else
  fail "invalid --version exit code was ${bad_version_code}, expected 78"
fi

if echo "$bad_version_output" | grep -qi "invalid"; then
  pass "invalid --version error message contains 'invalid'"
else
  fail "invalid --version error message missing 'invalid'"
fi

# ---------------------------------------------------------------------------
# Test 8: shim pin validation — bad pin in installed shim exits 78
# ---------------------------------------------------------------------------
echo ""
echo "Test 8: shim rejects invalid pin (exits 78)"

TMP_SHIM="$(mktemp -d)"
mkdir -p "${TMP_SHIM}/versions/v1.0.0-rc2"

# Install shim without clone/verify by overriding those functions
test_shim_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { : ; }\n'
  printf 'main "$@"\n'
} > "$test_shim_script"
chmod +x "$test_shim_script"
bash "$test_shim_script" --target "$TMP_SHIM" --version v1.0.0-rc2 >/dev/null 2>&1

if [ -x "${TMP_SHIM}/bin/vnx" ]; then
  pass "shim written to ${TMP_SHIM}/bin/vnx"

  # Create .vnx-version with bad pin and test from that dir
  TMP_BAD_PIN_DIR="$(mktemp -d)"
  printf '../etc/passwd\n' > "${TMP_BAD_PIN_DIR}/.vnx-version"

  shim_exit_code=0
  (cd "$TMP_BAD_PIN_DIR" && bash "${TMP_SHIM}/bin/vnx" 2>/dev/null) || shim_exit_code=$?

  if [ "$shim_exit_code" -eq 78 ]; then
    pass "shim rejects '../etc/passwd' pin with exit 78"
  else
    fail "shim exit code was ${shim_exit_code}, expected 78 for bad pin"
  fi

  rm -rf "$TMP_BAD_PIN_DIR"
else
  fail "shim not installed at ${TMP_SHIM}/bin/vnx"
fi

rm -rf "$TMP_SHIM" "$test_shim_script"

# ---------------------------------------------------------------------------
# Test 9: rollback restores previous 'current' symlink on install failure
# ---------------------------------------------------------------------------
echo ""
echo "Test 9: rollback restores previous symlink on failure"

TMP_ROLLBACK="$(mktemp -d)"
OLD_VER="v0.9.0"
mkdir -p "${TMP_ROLLBACK}/versions/${OLD_VER}"
ln -sfn "${TMP_ROLLBACK}/versions/${OLD_VER}" "${TMP_ROLLBACK}/current"

test_rollback_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { return 1; }\n'  # force failure after symlink swap
  printf 'main "$@"\n'
} > "$test_rollback_script"
chmod +x "$test_rollback_script"

bash "$test_rollback_script" --target "$TMP_ROLLBACK" --version v1.0.0-rc2 >/dev/null 2>&1 || true

current_target="$(readlink "${TMP_ROLLBACK}/current" 2>/dev/null || echo "MISSING")"
if echo "$current_target" | grep -qF "$OLD_VER"; then
  pass "rollback restored previous symlink (${OLD_VER})"
else
  fail "rollback did not restore previous symlink (got: ${current_target})"
fi

rm -rf "$TMP_ROLLBACK" "$test_rollback_script"

# ---------------------------------------------------------------------------
# Test 10: shim install uses atomic tempfile (no leftover .tmp. files)
# ---------------------------------------------------------------------------
echo ""
echo "Test 10: shim install leaves no temp files"

TMP_ATOMIC="$(mktemp -d)"
mkdir -p "${TMP_ATOMIC}/versions/v1.0.0-rc2"

test_atomic_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { : ; }\n'
  printf 'main "$@"\n'
} > "$test_atomic_script"
chmod +x "$test_atomic_script"

bash "$test_atomic_script" --target "$TMP_ATOMIC" --version v1.0.0-rc2 >/dev/null 2>&1

if [ -x "${TMP_ATOMIC}/bin/vnx" ]; then
  pass "shim exists and is executable"
else
  fail "shim missing or not executable"
fi

leftover_tmp="$(ls "${TMP_ATOMIC}/bin/"vnx.tmp.* 2>/dev/null || true)"
if [ -z "$leftover_tmp" ]; then
  pass "no leftover .tmp. files in shim dir"
else
  fail "leftover temp files found: ${leftover_tmp}"
fi

rm -rf "$TMP_ATOMIC" "$test_atomic_script"

# ---------------------------------------------------------------------------
# Test 11: macOS-like fallback (mv without -T) uses unlink+ln correctly
# ---------------------------------------------------------------------------
echo ""
echo "Test 11: macOS-like fallback: mock mv without -T support"

TMP_MACOS="$(mktemp -d)"
OLD_VER_MAC="v0.9.0"
NEW_VER_MAC="v1.0.0-rc2"
mkdir -p "${TMP_MACOS}/versions/${OLD_VER_MAC}"
mkdir -p "${TMP_MACOS}/versions/${NEW_VER_MAC}"
ln -sfn "${TMP_MACOS}/versions/${OLD_VER_MAC}" "${TMP_MACOS}/current"

# Fake mv that rejects -T to simulate macOS/BSD behaviour
FAKE_MV_DIR="$(mktemp -d)"
cat > "${FAKE_MV_DIR}/mv" <<'FAKEMV'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    -fT|-T) exit 1 ;;
  esac
done
exec /bin/mv "$@"
FAKEMV
chmod +x "${FAKE_MV_DIR}/mv"

test_macos_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { : ; }\n'
  printf 'main "$@"\n'
} > "$test_macos_script"
chmod +x "$test_macos_script"

macos_exit=0
PATH="${FAKE_MV_DIR}:${PATH}" bash "$test_macos_script" \
  --target "$TMP_MACOS" --version "$NEW_VER_MAC" >/dev/null 2>&1 || macos_exit=$?

if [ "$macos_exit" -eq 0 ]; then
  pass "macOS fallback exits 0"
else
  fail "macOS fallback exit code was ${macos_exit}, expected 0"
fi

current_target="$(readlink "${TMP_MACOS}/current" 2>/dev/null || echo "MISSING")"
if echo "$current_target" | grep -qF "$NEW_VER_MAC"; then
  pass "symlink correctly points to new version after macOS fallback"
else
  fail "symlink target after macOS fallback: ${current_target} (expected: ${NEW_VER_MAC})"
fi

leftover="$(ls "${TMP_MACOS}/"current.tmp.* 2>/dev/null || true)"
if [ -z "$leftover" ]; then
  pass "no leftover temp symlinks after macOS fallback"
else
  fail "leftover temp symlinks found: ${leftover}"
fi

rm -rf "$TMP_MACOS" "$FAKE_MV_DIR" "$test_macos_script"

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
