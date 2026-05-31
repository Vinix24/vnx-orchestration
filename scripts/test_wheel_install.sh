#!/usr/bin/env bash
# test_wheel_install.sh — fresh-venv packaging smoke test (PR-PIP-1 + PR-PIP-2
# acceptance gate).
#
# Builds the wheel, installs it into a SCRATCH virtualenv, and proves the
# installed artifact is a functional engine — not a hollow CLI shim:
#   1. `vnx --version` prints the single-sourced version (not 0.9.0 / unknown)
#   2. `vnx doctor` exits 0 against a freshly `vnx init`-ed scratch project
#   3. VNX_HOME resolves into site-packages and a schema-reading code path
#      (quality_db_init.bootstrap_qi_db, which reads VNX_HOME/schemas/*.sql)
#      succeeds against a scratch DB
#
# Then, in a PRISTINE HOME (no ~/.vnx-system, no ~/.vnx-data), proves the
# pip-native state layout (PR-PIP-2):
#   4. `vnx init` creates NO project-local .vnx-data/ (in-project footprint < 10 KB)
#   5. runtime state lands in the XDG user-data-dir (~/.local/share/vnx/<id>)
#   6. `vnx doctor` reports a RECOGNIZED packaged install (not "no install
#      detected"), with state resolved OUTSIDE the package, and exits 0
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
# PR-PIP-REPACKAGE: the engine ships under the vnx_orchestration namespace
# package, so wheel members are prefixed vnx_orchestration/ (not bare top-level).
log "verifying engine payload in wheel ..."
for needle in \
    "vnx_orchestration/scripts/lib/vnx_paths.py" \
    "vnx_orchestration/schemas/quality_intelligence.sql" \
    "vnx_orchestration/schemas/migrations/" \
    "vnx_orchestration/skills/" ; do
    "$PYTHON" -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); names=z.namelist(); sys.exit(0 if any(n.startswith(sys.argv[2]) or n==sys.argv[2] for n in names) else 1)" \
        "$WHEEL" "$needle" \
        || fail "wheel missing engine payload: $needle"
done
ok "wheel ships vnx_orchestration/{scripts,schemas/*.sql,schemas/migrations,skills}"

# --- top_level.txt namespace assertion (PR-PIP-REPACKAGE core gate) ---------
# The wheel MUST NOT squat the PyPI namespace with bare engine dirs. top_level
# must be exactly {vnx_cli, vnx_orchestration} — no bare scripts/schemas/
# configs/hooks/templates/skills as top-level packages.
log "verifying top_level.txt carries no bare engine packages ..."
"$PYTHON" - "$WHEEL" <<'PYEOF' || fail "top_level.txt namespace assertion failed"
import sys
import zipfile

wheel = sys.argv[1]
z = zipfile.ZipFile(wheel)
top_level_names = [n for n in z.namelist() if n.endswith("/top_level.txt")]
if not top_level_names:
    print("FAIL: wheel has no top_level.txt", file=sys.stderr)
    sys.exit(1)
tops = set(z.read(top_level_names[0]).decode("utf-8").split())
forbidden = {"scripts", "schemas", "configs", "hooks", "templates", "skills"}
squatters = tops & forbidden
if squatters:
    print(f"FAIL: top_level.txt squats bare engine packages: {sorted(squatters)}", file=sys.stderr)
    sys.exit(1)
if tops != {"vnx_cli", "vnx_orchestration"}:
    print(f"FAIL: top_level.txt is {sorted(tops)}, expected ['vnx_cli', 'vnx_orchestration']", file=sys.stderr)
    sys.exit(1)
print(f"OK: top_level.txt = {sorted(tops)}")
PYEOF
ok "top_level.txt = {vnx_cli, vnx_orchestration} — no bare engine packages"

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
NORM_VER="$(printf '%s' "$RAW_VER" | tr -d '-')"   # PEP440-ish: strip hyphens so 1.0.0 stays 1.0.0

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

from vnx_cli import _engine

# Engine root: <site-packages>/vnx_orchestration in a namespaced wheel
# (PR-PIP-REPACKAGE). Resolve via the shipped helper rather than assuming a
# bare top-level scripts/ sibling of vnx_cli/.
root = _engine.engine_root()
if "site-packages" not in str(root) and "dist-packages" not in str(root):
    print(f"FAIL: engine not loaded from an installed location: {root}", file=sys.stderr)
    sys.exit(1)

# Bootstrap the engine's lib dir exactly as the CLI commands do.
_engine.ensure_engine_on_path()

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

# --- PR-PIP-2: clean-HOME footprint + state-root + recognized install -------
# Re-run init + doctor in a PRISTINE HOME (no ~/.vnx-system, no ~/.vnx-data) so
# the pip-native layout is proven without any central/dev-checkout layout in
# scope. This is the PR-PIP-2 acceptance gate.
log "PR-PIP-2: clean-HOME init + doctor (state out of project) ..."

CLEAN_HOME="$TMPROOT/clean-home"
CLEAN_PROJECT="$TMPROOT/clean-project"
mkdir -p "$CLEAN_HOME" "$CLEAN_PROJECT"

# Pristine env: like clean_env, but also pins HOME to an empty scratch dir and
# strips the user-data-dir + project-id inputs so resolution is driven only by
# the clean HOME (→ XDG default ~/.local/share/vnx/<id>).
pip2_env() {
    env -u PYTHONPATH -u VNX_HOME -u VNX_BIN -u VNX_EXECUTABLE \
        -u VNX_DATA_DIR -u VNX_STATE_DIR -u VNX_DATA_DIR_EXPLICIT \
        -u VNX_DATA_HOME -u XDG_DATA_HOME -u VNX_PROJECT_ID \
        -u PROJECT_ROOT -u VNX_PROJECT_ROOT -u VNX_CANONICAL_ROOT \
        HOME="$CLEAN_HOME" "$@"
}

pip2_env "$VENV/bin/vnx" init --project-dir "$CLEAN_PROJECT" >"$TMPROOT/pip2_init.log" 2>&1 \
    || { cat "$TMPROOT/pip2_init.log" >&2; fail "clean-HOME vnx init failed"; }

# Assertion 1: NO project-local .vnx-data/ was created (clean footprint).
[ ! -e "$CLEAN_PROJECT/.vnx-data" ] \
    || fail "clean-HOME vnx init created a project-local .vnx-data/ (expected none)"
ok "vnx init created no project-local .vnx-data/"

# Assertion 2: in-project footprint < 10 KB (tracked config only).
FOOT_BYTES="$(pip2_env "$VENV/bin/python" - "$CLEAN_PROJECT" <<'PYEOF'
import sys
from pathlib import Path
root = Path(sys.argv[1])
total = sum(
    f.stat().st_size for f in root.rglob("*")
    if f.is_file() and ".git" not in f.parts
)
print(total)
PYEOF
)"
[ "$FOOT_BYTES" -lt 10240 ] \
    || fail "in-project footprint ${FOOT_BYTES} bytes >= 10 KB (expected clean footprint)"
ok "in-project footprint ${FOOT_BYTES} bytes (< 10 KB)"

# Resolve the project_id init wrote, derive the expected XDG state root.
PID2="$(head -1 "$CLEAN_PROJECT/.vnx-project-id" 2>/dev/null | tr -d '[:space:]')"
[ -n "$PID2" ] || fail "vnx init did not write a .vnx-project-id marker"
EXPECT_STATE="$CLEAN_HOME/.local/share/vnx/$PID2"

# Assertion 3: state landed in the XDG user-data-dir, populated with the tree.
[ -d "$EXPECT_STATE/dispatches/pending" ] \
    || fail "runtime state not at XDG dir $EXPECT_STATE (dispatches/pending missing)"
ok "runtime state at XDG user-data-dir: $EXPECT_STATE"

# Assertion 4: doctor reports a RECOGNIZED (packaged) install + state outside
# the package, and exits 0. Parse the structured --json output.
# Capture the rc without tripping `set -e` (which would abort before we can
# print the diagnostic). `|| DOCTOR_RC=$?` runs only on a non-zero exit.
DOCTOR_RC=0
pip2_env "$VENV/bin/vnx" doctor --project-dir "$CLEAN_PROJECT" --json \
    >"$TMPROOT/pip2_doctor.json" 2>"$TMPROOT/pip2_doctor.err" || DOCTOR_RC=$?
[ "$DOCTOR_RC" -eq 0 ] \
    || { cat "$TMPROOT/pip2_doctor.json" "$TMPROOT/pip2_doctor.err" >&2; fail "clean-HOME vnx doctor exited $DOCTOR_RC (expected 0)"; }

pip2_env "$VENV/bin/python" - "$TMPROOT/pip2_doctor.json" "$EXPECT_STATE" "$CLEAN_PROJECT" <<'PYEOF' \
    || fail "clean-HOME doctor assertions failed (see above)"
import json
import sys
from pathlib import Path

doctor_json, expect_state, project_dir = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.loads(Path(doctor_json).read_text(encoding="utf-8"))
checks = {c["name"]: c for c in data["checks"]}


def die(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


# No check may report the "no install detected" regression.
for c in data["checks"]:
    if "no VNX install detected" in c["detail"]:
        die(f"doctor still reports 'no VNX install detected' on {c['name']}: {c['detail']}")

mode = checks.get("install:mode")
if mode is None:
    die("doctor emitted no install:mode check")
if mode["status"] != "PASS":
    die(f"install:mode not PASS: {mode['status']} — {mode['detail']}")
if "packaged" not in mode["detail"]:
    die(f"install:mode did not detect a packaged (site-packages) install: {mode['detail']}")

state_loc = checks.get("state:location")
if state_loc is None:
    die("doctor emitted no state:location check")
if state_loc["status"] != "PASS":
    die(f"state:location not PASS (state inside package/VNX_HOME?): {state_loc['detail']}")

data_root_chk = checks.get("dir:data-root")
if data_root_chk is None:
    die("doctor emitted no dir:data-root check")
if data_root_chk["status"] != "PASS":
    die(f"dir:data-root not PASS: {data_root_chk['detail']}")
# The reported data root must be the XDG dir, never inside the project map.
resolved = data_root_chk["detail"]
if Path(resolved).resolve() != Path(expect_state).resolve():
    die(f"data root {resolved!r} != expected XDG dir {expect_state!r}")
if Path(resolved).resolve().is_relative_to(Path(project_dir).resolve()):
    die(f"data root {resolved!r} resolved INSIDE the project map {project_dir!r}")

print(f"OK: doctor recognizes packaged install; state root {resolved} outside the project")
PYEOF
ok "vnx doctor: recognized packaged install, state outside project, exit 0"

# Assertion 5: version still single-sourced under the clean HOME.
CLEAN_VER="$(pip2_env "$VENV/bin/vnx" --version)"
[ "$CLEAN_VER" = "vnx $META_VER" ] \
    || fail "clean-HOME vnx --version ('$CLEAN_VER') != 'vnx $META_VER'"
ok "vnx --version correct in clean HOME: $CLEAN_VER"

echo
ok "PR-PIP-1 wheel-install smoke PASSED ($(basename "$WHEEL"), v$META_VER)"
ok "PR-PIP-2 clean-HOME state-root + footprint smoke PASSED (state: $EXPECT_STATE)"
