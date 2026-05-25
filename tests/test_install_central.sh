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
# Test 12: swap_symlink happy path — succeeds, symlink updated, no temp files
# ---------------------------------------------------------------------------
echo ""
echo "Test 12: swap_symlink atomic swap — existing target replaced, no temp files"

TMP_12="$(mktemp -d)"
OLD_VER_12="v0.9.0"
NEW_VER_12="v1.0.0-rc2"
mkdir -p "${TMP_12}/versions/${OLD_VER_12}" "${TMP_12}/versions/${NEW_VER_12}"
ln -sn "${TMP_12}/versions/${OLD_VER_12}" "${TMP_12}/current"

test_12_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"
  printf 'DRY_RUN=false\n'
  printf 'swap_symlink "%s/current" "%s/versions/%s"\n' "$TMP_12" "$TMP_12" "$NEW_VER_12"
} > "$test_12_script"

test_12_exit=0
bash "$test_12_script" >/dev/null 2>&1 || test_12_exit=$?

if [ "$test_12_exit" -eq 0 ]; then
  pass "swap_symlink exits 0 on happy path"
else
  fail "swap_symlink exited ${test_12_exit}, expected 0"
fi

current_12="$(readlink "${TMP_12}/current" 2>/dev/null || echo "MISSING")"
if echo "$current_12" | grep -qF "$NEW_VER_12"; then
  pass "current symlink points to new version after swap"
else
  fail "current symlink not updated: ${current_12}"
fi

leftover_12="$(ls "${TMP_12}/"current.swap.* 2>/dev/null || true)"
if [ -z "$leftover_12" ]; then
  pass "no leftover .swap. temp files after successful swap"
else
  fail "leftover temp files found: ${leftover_12}"
fi

rm -rf "$TMP_12" "$test_12_script"

# ---------------------------------------------------------------------------
# Test 13: swap_symlink — ln fails (read-only parent dir) → temp cleaned, current unchanged
# ---------------------------------------------------------------------------
echo ""
echo "Test 13: swap_symlink ln failure — tempfile cleaned up, current symlink unchanged"

TMP_13="$(mktemp -d)"
OLD_VER_13="v0.9.0"
NEW_VER_13="v1.0.0-rc2"
mkdir -p "${TMP_13}/versions/${OLD_VER_13}" "${TMP_13}/versions/${NEW_VER_13}"
ln -sn "${TMP_13}/versions/${OLD_VER_13}" "${TMP_13}/current"
chmod 555 "$TMP_13"  # make parent dir read-only so ln -sn fails

test_13_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"
  printf 'DRY_RUN=false\n'
  printf 'swap_symlink "%s/current" "%s/versions/%s"\n' "$TMP_13" "$TMP_13" "$NEW_VER_13"
} > "$test_13_script"

test_13_exit=0
bash "$test_13_script" >/dev/null 2>&1 || test_13_exit=$?

chmod 755 "$TMP_13"  # restore for cleanup

if [ "$test_13_exit" -eq 75 ]; then
  pass "swap_symlink returns 75 (EX_TEMPFAIL) when ln fails"
else
  fail "expected exit 75, got ${test_13_exit}"
fi

current_13="$(readlink "${TMP_13}/current" 2>/dev/null || echo "MISSING")"
if echo "$current_13" | grep -qF "$OLD_VER_13"; then
  pass "current symlink unchanged after swap_symlink failure"
else
  fail "current symlink was modified during failure: ${current_13}"
fi

leftover_13="$(ls "${TMP_13}/"current.swap.* 2>/dev/null || true)"
if [ -z "$leftover_13" ]; then
  pass "no leftover .swap. temp files after ln failure"
else
  fail "leftover temp files found after failure: ${leftover_13}"
fi

rm -rf "$TMP_13" "$test_13_script"

# ---------------------------------------------------------------------------
# Test 14: cleanup_on_failure — rollback fails → exits 70, FATAL logged
# ---------------------------------------------------------------------------
echo ""
echo "Test 14: cleanup_on_failure broken rollback — exits 70, FATAL message logged"

TMP_14="$(mktemp -d)"
chmod 555 "$TMP_14"  # read-only so swap_symlink's ln fails inside cleanup_on_failure

test_14_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"
  printf 'DRY_RUN=false\n'
  printf 'TARGET_DIR="%s"\n' "$TMP_14"
  printf '_PREVIOUS_TARGET="%s/some-version"\n' "$TMP_14"
  printf 'cleanup_on_failure\n'
} > "$test_14_script"

test_14_exit=0
test_14_output="$(bash "$test_14_script" 2>&1)" || test_14_exit=$?

chmod 755 "$TMP_14"  # restore for cleanup

if [ "$test_14_exit" -eq 70 ]; then
  pass "cleanup_on_failure exits 70 (EX_SOFTWARE) when rollback fails"
else
  fail "expected exit 70, got ${test_14_exit}"
fi

if echo "$test_14_output" | grep -qi "FATAL"; then
  pass "cleanup_on_failure logs FATAL message on rollback failure"
else
  fail "FATAL message not found in output: ${test_14_output}"
fi

rm -rf "$TMP_14" "$test_14_script"

# ---------------------------------------------------------------------------
# Test 15: shim_no_vnx_cli_reference — installed shim must not contain 'vnx-cli'
# ---------------------------------------------------------------------------
echo ""
echo "Test 15: shim_no_vnx_cli_reference — installed shim must not reference 'vnx-cli'"

TMP_15="$(mktemp -d)"
mkdir -p "${TMP_15}/versions/v1.0.0-rc3"

test_15_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { : ; }\n'
  printf 'main "$@"\n'
} > "$test_15_script"
chmod +x "$test_15_script"

bash "$test_15_script" --target "$TMP_15" --version v1.0.0-rc3 >/dev/null 2>&1

if [ -f "${TMP_15}/bin/vnx" ]; then
  if grep -q 'vnx-cli' "${TMP_15}/bin/vnx" 2>/dev/null; then
    fail "shim still contains references to 'vnx-cli'"
  else
    pass "shim contains no 'vnx-cli' references"
  fi
else
  fail "shim not found at ${TMP_15}/bin/vnx"
fi

rm -rf "$TMP_15" "$test_15_script"

# ---------------------------------------------------------------------------
# Test 16: shim_execs_correct_binary — installed shim exec target is bin/vnx
# ---------------------------------------------------------------------------
echo ""
echo "Test 16: shim_execs_correct_binary — shim exec line points to bin/vnx not bin/vnx-cli"

TMP_16="$(mktemp -d)"
mkdir -p "${TMP_16}/versions/v1.0.0-rc3"

test_16_script="$(mktemp)"
{
  sed '$ d' "$SCRIPT"  # strip last line: 'main "$@"'
  printf 'check_prereqs() { : ; }\n'
  printf 'clone_version() { : ; }\n'
  printf 'verify_install() { : ; }\n'
  printf 'main "$@"\n'
} > "$test_16_script"
chmod +x "$test_16_script"

bash "$test_16_script" --target "$TMP_16" --version v1.0.0-rc3 >/dev/null 2>&1

if [ -f "${TMP_16}/bin/vnx" ]; then
  # Shim must contain 'exec "${VNX_HOME}/bin/vnx"' (correct) and not 'bin/vnx-cli'
  if grep -q 'exec.*bin/vnx"' "${TMP_16}/bin/vnx" && ! grep -q 'bin/vnx-cli' "${TMP_16}/bin/vnx"; then
    pass "shim exec line correctly references bin/vnx"
  else
    exec_line="$(grep 'exec.*bin/vnx' "${TMP_16}/bin/vnx" || echo '(not found)')"
    fail "shim exec line incorrect: ${exec_line}"
  fi
else
  fail "shim not found at ${TMP_16}/bin/vnx"
fi

rm -rf "$TMP_16" "$test_16_script"

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
