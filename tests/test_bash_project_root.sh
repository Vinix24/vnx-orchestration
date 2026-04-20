#!/usr/bin/env bash
# Tests for scripts/lib/vnx_resolve_root.sh
# Verifies that the bash project-root helper resolves paths correctly via git,
# honors VNX_DATA_DIR_EXPLICIT=1 for overrides, falls back to VNX_CANONICAL_ROOT,
# and returns 1 when neither git nor the env fallback is available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../scripts/lib/vnx_resolve_root.sh"

PASS=0
FAIL=0

assert_eq() {
    local expected="$1" actual="$2" label="$3"
    if [ "$expected" = "$actual" ]; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label (expected='$expected' actual='$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local needle="$1" haystack="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label (expected substring='$needle' in='$haystack')"
        FAIL=$((FAIL + 1))
    fi
}

assert_nonempty() {
    local val="$1" label="$2"
    if [ -n "$val" ]; then
        echo "PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $label (expected non-empty, got empty)"
        FAIL=$((FAIL + 1))
    fi
}

# ── Test 1: resolution from existing git repo ────────────────────────────────
# Source the helper inside a subshell so exported vars don't leak between tests.
RESULT=$(
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    # shellcheck source=../scripts/lib/vnx_resolve_root.sh
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    printf '%s' "$VNX_PROJECT_ROOT"
)
assert_nonempty "$RESULT" "vnx_resolve_project_root resolves from caller file in git repo"

# ── Test 2: VNX_PROJECT_ROOT is the actual git toplevel ─────────────────────
RESULT=$(
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    expected_root="$(git rev-parse --show-toplevel)"
    if [ "$VNX_PROJECT_ROOT" = "$expected_root" ]; then
        printf 'ok'
    else
        printf 'fail:%s!=%s' "$VNX_PROJECT_ROOT" "$expected_root"
    fi
)
assert_eq "ok" "$RESULT" "VNX_PROJECT_ROOT matches git rev-parse --show-toplevel"

# ── Test 3: derived dirs are rooted under VNX_PROJECT_ROOT ──────────────────
RESULT=$(
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    vnx_resolve_data_dir
    vnx_resolve_state_dir
    vnx_resolve_dispatch_dir
    if [[ "$VNX_DATA_DIR" == "${VNX_PROJECT_ROOT}/.vnx-data" ]] &&
       [[ "$VNX_STATE_DIR" == "${VNX_DATA_DIR}/state" ]] &&
       [[ "$VNX_DISPATCH_DIR" == "${VNX_DATA_DIR}/dispatches" ]]; then
        printf 'ok'
    else
        printf 'fail: data=%s state=%s dispatch=%s root=%s' "$VNX_DATA_DIR" "$VNX_STATE_DIR" "$VNX_DISPATCH_DIR" "$VNX_PROJECT_ROOT"
    fi
)
assert_eq "ok" "$RESULT" "VNX_DATA_DIR/STATE_DIR/DISPATCH_DIR derived from VNX_PROJECT_ROOT"

# ── Test 4: VNX_DATA_DIR_EXPLICIT=1 honors override ─────────────────────────
RESULT=$(
    unset VNX_PROJECT_ROOT VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT 2>/dev/null || true
    export VNX_DATA_DIR="/custom/data/dir"
    export VNX_DATA_DIR_EXPLICIT=1
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    vnx_resolve_data_dir
    printf '%s' "$VNX_DATA_DIR"
)
assert_eq "/custom/data/dir" "$RESULT" "VNX_DATA_DIR_EXPLICIT=1 honors VNX_DATA_DIR override"

# ── Test 5: VNX_DATA_DIR without EXPLICIT flag is ignored ───────────────────
RESULT=$(
    unset VNX_PROJECT_ROOT VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    export VNX_DATA_DIR="/stale/inherited/dir"
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    vnx_resolve_data_dir 2>/dev/null
    if [[ "$VNX_DATA_DIR" == "${VNX_PROJECT_ROOT}/.vnx-data" ]]; then
        printf 'ok'
    else
        printf 'fail: %s' "$VNX_DATA_DIR"
    fi
)
assert_eq "ok" "$RESULT" "VNX_DATA_DIR without EXPLICIT flag is ignored (uses git root)"

# ── Test 6: VNX_DATA_DIR without EXPLICIT emits DeprecationWarning ───────────
WARN=$(
    unset VNX_PROJECT_ROOT VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    export VNX_DATA_DIR="/stale/dir"
    source "$HELPER"
    vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
    vnx_resolve_data_dir 2>&1 1>/dev/null
)
assert_contains "DeprecationWarning" "$WARN" "VNX_DATA_DIR without EXPLICIT emits DeprecationWarning"

# ── Test 7: VNX_CANONICAL_ROOT fallback in non-git tempdir ──────────────────
RESULT=$(
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    export VNX_CANONICAL_ROOT="$tmpdir"
    cd "$tmpdir"
    source "$HELPER"
    # Call without caller arg so step 1 is skipped cleanly, CWD is non-git tmpdir
    vnx_resolve_project_root "" 2>/dev/null
    expected="$(cd "$tmpdir" && pwd -P)"
    if [ "$VNX_PROJECT_ROOT" = "$expected" ]; then
        printf 'ok'
    else
        printf 'fail: %s' "$VNX_PROJECT_ROOT"
    fi
)
assert_eq "ok" "$RESULT" "VNX_CANONICAL_ROOT used as fallback when outside git repo"

# ── Test 8: VNX_CANONICAL_ROOT fallback emits DeprecationWarning ────────────
WARN=$(
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    export VNX_CANONICAL_ROOT="$tmpdir"
    cd "$tmpdir"
    source "$HELPER"
    vnx_resolve_project_root "" 2>&1 1>/dev/null
)
assert_contains "DeprecationWarning" "$WARN" "VNX_CANONICAL_ROOT fallback emits DeprecationWarning"

# ── Test 9: returns 1 with no git and no VNX_CANONICAL_ROOT ─────────────────
RC=0
(
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    cd "$tmpdir"
    source "$HELPER"
    set +e
    vnx_resolve_project_root "" 2>/dev/null
    exit $?
) || RC=$?
assert_eq "1" "$RC" "returns exit code 1 when no git repo and VNX_CANONICAL_ROOT unset"

# ── Test 10: resolution from temp git repo ───────────────────────────────────
RESULT=$(
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT
    git init -q "$tmpdir"
    unset VNX_PROJECT_ROOT VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_CANONICAL_ROOT VNX_DATA_DIR_EXPLICIT 2>/dev/null || true
    cd "$tmpdir"
    source "$HELPER"
    vnx_resolve_project_root "" 2>/dev/null
    # Canonicalize both paths for comparison
    expected="$(cd "$tmpdir" && pwd -P)"
    actual="$(cd "$VNX_PROJECT_ROOT" && pwd -P)"
    if [ "$expected" = "$actual" ]; then
        printf 'ok'
    else
        printf 'fail: expected=%s actual=%s' "$expected" "$actual"
    fi
)
assert_eq "ok" "$RESULT" "resolves correctly from a fresh git init temp repo"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
