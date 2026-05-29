#!/usr/bin/env bash
# pip_install_smoke.sh — Profile D: pip-install mode smoke test.
#
# Catches the class of bug where dev-checkout path resolution accidentally
# resolves so CI stays green, but a real pip install breaks (e.g. commands
# that hardcode Path(__file__).parents[N] instead of going through _engine).
#
# Usage:
#   bash scripts/ci/pip_install_smoke.sh           # build wheel first
#   bash scripts/ci/pip_install_smoke.sh WHEEL.whl # skip build
#
# Exit 0 = all checks passed. Exit 1 = one or more failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

_pass() { echo "[PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "[FAIL] $1"; FAIL=$((FAIL + 1)); }
_info() { echo "[INFO] $1"; }
_sep()  { echo "------------------------------------------------------------"; }

# Work entirely outside the repo so there is no accidental path bleed.
WORK_DIR="$(mktemp -d /tmp/vnx-pip-smoke.XXXXXX)"
VENV_DIR="$WORK_DIR/venv"
WHEEL_DIR="$WORK_DIR/wheel"
RUN_DIR="$WORK_DIR/project"
mkdir -p "$WHEEL_DIR" "$RUN_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

_sep
_info "Work dir: $WORK_DIR"

# ── Step 1: Build or accept wheel ────────────────────────────────────────────
if [ -n "${1:-}" ] && [ -f "$1" ]; then
    WHEEL_FILE="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1")"
    _info "Using supplied wheel: $WHEEL_FILE"
else
    _info "Building wheel from: $REPO_ROOT"
    cd "$REPO_ROOT"
    python3 -m pip install --quiet --upgrade build
    python3 -m build --wheel --outdir "$WHEEL_DIR" .
    WHEEL_FILE="$(ls "$WHEEL_DIR"/*.whl | head -1)"
    _info "Built: $(basename "$WHEEL_FILE")"
fi
_sep

# ── Step 2: Packaging hygiene (no __pycache__ / .pyc in the wheel) ───────────
_info "Hygiene check: __pycache__ / .pyc in wheel"
python3 - "$WHEEL_FILE" <<'PYEOF'
import sys
import zipfile

wheel = sys.argv[1]
bad = [
    n for n in zipfile.ZipFile(wheel).namelist()
    if "__pycache__" in n or n.endswith(".pyc")
]
if bad:
    print(f"[HYGIENE] {len(bad)} forbidden entries found:")
    for entry in bad[:10]:
        print(f"  {entry}")
    sys.exit(2)
else:
    print(f"[HYGIENE] clean — 0 __pycache__/.pyc entries in wheel")
PYEOF
HYGIENE_RC=$?
if [ "$HYGIENE_RC" -eq 2 ]; then
    _fail "Wheel contains __pycache__/__pycache__ or .pyc files (see above)"
elif [ "$HYGIENE_RC" -ne 0 ]; then
    _fail "Hygiene check script error (rc=$HYGIENE_RC)"
else
    _pass "Wheel has no __pycache__ or .pyc pollution"
fi
_sep

# ── Step 3: Fresh venv + pip install ─────────────────────────────────────────
_info "Creating fresh venv at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet "$WHEEL_FILE"

VNX="$VENV_DIR/bin/vnx"
if [ ! -x "$VNX" ]; then
    _fail "vnx binary missing after pip install — aborting"
    exit 1
fi
_pass "vnx binary installed: $VNX"
_sep

# ── Helpers ───────────────────────────────────────────────────────────────────
# run_help LABEL CMD [ARGS...]
#   Requires exit 0 AND no Python crash (traceback / ModuleNotFoundError).
run_help() {
    local label="$1"; shift
    local out exit_code=0
    out="$("$@" 2>&1)" || exit_code=$?
    local has_tb=0 has_mne=0
    echo "$out" | grep -q "Traceback (most recent call last)" && has_tb=1 || true
    echo "$out" | grep -q "ModuleNotFoundError"              && has_mne=1 || true
    if [ "$has_tb" -eq 1 ] || [ "$has_mne" -eq 1 ]; then
        _fail "$label — Python crash (traceback or ModuleNotFoundError)"
        echo "$out" | tail -15
    elif [ "$exit_code" -ne 0 ]; then
        _fail "$label — exit $exit_code (expected 0 for --help)"
        echo "$out" | tail -5
    else
        _pass "$label"
    fi
}

# run_real LABEL CMD [ARGS...]
#   Graceful non-zero exit is OK (e.g. "not initialized").
#   Python crash (traceback / ModuleNotFoundError) is FAIL.
run_real() {
    local label="$1"; shift
    local out exit_code=0
    out="$("$@" 2>&1)" || exit_code=$?
    local has_tb=0 has_mne=0
    echo "$out" | grep -q "Traceback (most recent call last)" && has_tb=1 || true
    echo "$out" | grep -q "ModuleNotFoundError"              && has_mne=1 || true
    if [ "$has_tb" -eq 1 ]; then
        _fail "$label — Python traceback"
        echo "$out" | tail -20
    elif [ "$has_mne" -eq 1 ]; then
        _fail "$label — ModuleNotFoundError"
        echo "$out" | grep -A 4 "ModuleNotFoundError" | head -10
    else
        _pass "$label (exit $exit_code)"
    fi
}

# All commands run from the temp project dir — never from the repo checkout.
cd "$RUN_DIR"

# ── Step 4: --help checks (must exit 0) ──────────────────────────────────────
_info "--- help checks (must exit 0 and not crash) ---"
run_help "vnx --help"        "$VNX" --help
run_help "vnx --version"     "$VNX" --version
run_help "vnx doctor --help" "$VNX" doctor --help
run_help "vnx init --help"   "$VNX" init --help
run_help "vnx pool --help"   "$VNX" pool --help
run_help "vnx track --help"  "$VNX" track --help
run_help "vnx dream --help"  "$VNX" dream --help
run_help "vnx status --help" "$VNX" status --help
_sep

# ── Step 5: Real invocations (graceful non-0 OK; crash = FAIL) ───────────────
_info "--- real invocations (graceful non-0 = PASS; traceback/ModuleNotFoundError = FAIL) ---"
run_real "vnx --version (real)"       "$VNX" --version
run_real "vnx doctor"                 "$VNX" doctor --project-dir "$RUN_DIR"
run_real "vnx init --non-interactive" "$VNX" init --non-interactive \
    --project-dir "$RUN_DIR" --project-id smoke
run_real "vnx dream status"           "$VNX" dream status --project-id smoke
run_real "vnx track list"             "$VNX" track list --project-id smoke
run_real "vnx pool status"            "$VNX" pool status --project smoke
_sep

# ── Summary ───────────────────────────────────────────────────────────────────
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    echo "[SMOKE] FAILED — $FAIL check(s) did not pass"
    exit 1
fi
echo "[SMOKE] ALL PASSED"
