#!/usr/bin/env bash
# test_wheel_install.sh — fresh-venv packaging smoke test (PR-PIP-1 acceptance gate).
#
# Builds the wheel, installs it into a SCRATCH virtualenv, and proves the
# installed artifact is a functional engine — not a hollow CLI shim:
#   1. `vnx --version` prints the single-sourced version (not 0.9.0 / unknown)
#   2. `vnx doctor` exits 0 against a freshly `vnx init`-ed scratch project
#   3. VNX_HOME resolves into site-packages and a schema-reading code path
#      (quality_db_init.bootstrap_qi_db, which reads VNX_HOME/schemas/*.sql)
#      succeeds against a scratch DB
#
# All venv invocations run from a NEUTRAL cwd with `python -P` and a sanitized
# environment so the source checkout (CWD, VNX_HOME, PYTHONPATH) cannot shadow
# the installed package.
#
# Exit codes:
#   0 — smoke passed
#   1 — smoke assertion failed
#   2 — preflight failure (missing build toolchain / unusable python)
#
# Env overrides:
#   SMOKE_PYTHON   python interpreter to build the venv (default: python3),
#                  must satisfy requires-python (>=3.11,<3.14)
#
# No external dependencies beyond the build toolchain (build, setuptools, wheel)
# and the runtime prereqs vnx doctor checks (git, jq).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

PYTHON="${SMOKE_PYTHON:-python3}"

# Sanitized environment for every venv invocation: strip the source checkout's
# VNX_* / PYTHONPATH so the installed wheel — not the repo — is exercised.
clean_env() {
    env -u PYTHONPATH -u VNX_HOME -u VNX_BIN -u VNX_EXECUTABLE \
        -u VNX_DATA_DIR -u VNX_STATE_DIR -u VNX_DATA_DIR_EXPLICIT \
        -u PROJECT_ROOT -u VNX_PROJECT_ROOT -u VNX_CANONICAL_ROOT \
        "$@"
}

log()  { printf '\033[94m[smoke]\033[0m %s\n' "$*"; }
ok()   { printf '\033[92m[ ok ]\033[0m %s\n' "$*"; }
fail() { printf '\033[91m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[smoke] python interpreter not found: $PYTHON" >&2
    exit 2
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)'; then
    echo "[smoke] $PYTHON does not satisfy requires-python (>=3.11,<3.14):" >&2
    "$PYTHON" --version >&2
    echo "[smoke] set SMOKE_PYTHON to a compatible interpreter" >&2
    exit 2
fi

if ! "$PYTHON" -c 'import build, setuptools, wheel' 2>/dev/null; then
    echo "[smoke] build toolchain missing — install with: $PYTHON -m pip install build setuptools wheel" >&2
    exit 2
fi

# --- workspace -------------------------------------------------------------
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/vnx-pip1-smoke.XXXXXX")"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

DIST_DIR="$TMPROOT/dist"
VENV="$TMPROOT/venv"
SCRATCH="$TMPROOT/project"
mkdir -p "$DIST_DIR" "$SCRATCH"

# --- build -----------------------------------------------------------------
log "building wheel (no build isolation) ..."
"$PYTHON" -m build --wheel --no-isolation --outdir "$DIST_DIR" "$REPO_ROOT" >"$TMPROOT/build.log" 2>&1 \
    || { cat "$TMPROOT/build.log" >&2; fail "wheel build failed"; }

WHEEL="$(ls "$DIST_DIR"/vnx_orchestration-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] || fail "no wheel produced in $DIST_DIR"
ok "built $(basename "$WHEEL")"

# --- engine-presence assertions (cheap, before install) --------------------
log "verifying engine payload in wheel ..."
for needle in \
    "scripts/lib/vnx_paths.py" \
    "schemas/quality_intelligence.sql" \
    "schemas/migrations/" \
    "skills/" ; do
    "$PYTHON" -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); names=z.namelist(); sys.exit(0 if any(n.startswith(sys.argv[2]) or n==sys.argv[2] for n in names) else 1)" \
        "$WHEEL" "$needle" \
        || fail "wheel missing engine payload: $needle"
done
ok "wheel ships scripts/, schemas/*.sql, schemas/migrations/, skills/"

# --- fresh venv + install --------------------------------------------------
log "creating fresh venv + installing wheel ..."
"$PYTHON" -m venv "$VENV"
clean_env "$VENV/bin/pip" install --no-cache-dir -q "$WHEEL" >"$TMPROOT/install.log" 2>&1 \
    || { cat "$TMPROOT/install.log" >&2; fail "pip install failed"; }
[ -x "$VENV/bin/vnx" ] || fail "console entry point vnx not installed"
ok "installed into $VENV"

# --- G4: version single-source --------------------------------------------
log "asserting version single-source ..."
META_VER="$(clean_env "$VENV/bin/python" -c 'from importlib.metadata import version; print(version("vnx-orchestration"))')"
CLI_VER="$(clean_env "$VENV/bin/vnx" --version)"
RAW_VER="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
NORM_VER="$(printf '%s' "$RAW_VER" | tr -d '-')"   # PEP440-ish: 1.0.0-rc3 -> 1.0.0rc3

[ "$CLI_VER" = "vnx $META_VER" ] || fail "vnx --version ('$CLI_VER') != metadata ('vnx $META_VER')"
[ "$META_VER" != "0.9.0" ] || fail "version still hardcoded 0.9.0 — drift not fixed"
case "$META_VER" in
    0.0.0*|unknown) fail "version unresolved ('$META_VER')" ;;
esac
[ "$META_VER" = "$NORM_VER" ] || fail "metadata version ('$META_VER') != normalized VERSION file ('$NORM_VER')"
ok "version single-sourced: $META_VER (CLI, metadata, and VERSION agree)"

# --- G3a: doctor exits 0 on a fresh project --------------------------------
log "asserting vnx init + vnx doctor (exit 0) ..."
clean_env "$VENV/bin/vnx" init --project-dir "$SCRATCH" >"$TMPROOT/init.log" 2>&1 \
    || { cat "$TMPROOT/init.log" >&2; fail "vnx init failed"; }
if ! clean_env "$VENV/bin/vnx" doctor --project-dir "$SCRATCH" >"$TMPROOT/doctor.log" 2>&1; then
    cat "$TMPROOT/doctor.log" >&2
    fail "vnx doctor did not exit 0 (note: requires git + jq in PATH)"
fi
ok "vnx doctor exit 0"

# --- G3b: packaged VNX_HOME + schema resolution ----------------------------
log "asserting packaged VNX_HOME + schema read (bootstrap_qi_db) ..."
PROOF="$TMPROOT/schema_proof.py"
cat > "$PROOF" <<'PYEOF'
import sys
from pathlib import Path

import vnx_cli

root = Path(vnx_cli.__file__).resolve().parent.parent
if "site-packages" not in str(root) and "dist-packages" not in str(root):
    print(f"FAIL: vnx_cli not loaded from an installed location: {root}", file=sys.stderr)
    sys.exit(1)

# Mirror how engine modules bootstrap their own lib dir.
sys.path.insert(0, str(root / "scripts"))
sys.path.insert(0, str(root / "scripts" / "lib"))

from vnx_paths import resolve_paths

vnx_home = Path(resolve_paths()["VNX_HOME"])
print(f"VNX_HOME -> {vnx_home}")
if vnx_home != root:
    print(f"FAIL: VNX_HOME ({vnx_home}) did not resolve to engine root ({root})", file=sys.stderr)
    sys.exit(1)

schema = vnx_home / "schemas" / "quality_intelligence.sql"
if not schema.is_file():
    print(f"FAIL: packaged schema missing: {schema}", file=sys.stderr)
    sys.exit(1)

import quality_db_init

db = Path(sys.argv[1]) / "qi.db"
if not quality_db_init.bootstrap_qi_db(db):
    print("FAIL: bootstrap_qi_db returned False", file=sys.stderr)
    sys.exit(1)
if not db.is_file():
    print(f"FAIL: scratch db not created: {db}", file=sys.stderr)
    sys.exit(1)

print("OK: VNX_HOME resolved into site-packages and packaged schema initialized a scratch DB")
sys.exit(0)
PYEOF

# Run from a NEUTRAL cwd with -P so the source checkout cannot shadow imports.
( cd "$SCRATCH" && clean_env "$VENV/bin/python" -P "$PROOF" "$SCRATCH" ) >"$TMPROOT/schema.log" 2>&1 \
    || { cat "$TMPROOT/schema.log" >&2; fail "packaged schema resolution failed"; }
grep -q '^VNX_HOME ->' "$TMPROOT/schema.log" && grep -q '^OK:' "$TMPROOT/schema.log" \
    || { cat "$TMPROOT/schema.log" >&2; fail "schema proof produced unexpected output"; }
ok "$(grep '^VNX_HOME ->' "$TMPROOT/schema.log")"
ok "packaged engine reads VNX_HOME/schemas and initializes a DB"

echo
ok "PR-PIP-1 wheel-install smoke PASSED ($(basename "$WHEEL"), v$META_VER)"
