#!/usr/bin/env bash
# scripts/local-ci.sh — Local mirror of .github/workflows/vnx-ci.yml gates.
#
# Runs every gate, collects pass/fail, never aborts on first failure.
# Prints a summary matrix. Exits 1 if ≥1 gate fails.
#
# Usage:
#   ./scripts/local-ci.sh [--base-ref <ref>] [--with-npm] [--help]
#
# Flags:
#   --base-ref <ref>  Git ref to diff against (default: origin/main)
#   --with-npm        Include dashboard-lint (npm ci is expensive; skipped by default
#                     unless dashboard/token-dashboard has TS changes on the diff)
#   --help            Show this message and exit
#
# Gates mirrored from vnx-ci.yml:
#   legacy-path-gate, profileA-doctor, profileA-state-integrity, profileA-tests,
#   trace-token-check, trace-token-tests, slug-match-gate, slug-match-tests,
#   profileC-path-resolution, profileC-docs-command, profileC-quickstart,
#   profileC-verify-closure, profileB-snapshot-integration,
#   lint-patterns, dashboard-lint (conditional), adr003-no-anthropic-sdk
#
# Root resolution: scripts/lib/vnx_resolve_root.sh (issue #225).
# Logs per gate:  /tmp/lci_<gate>.log
set -uo pipefail

# ─── Root resolution ──────────────────────────────────────────────────────────
SCRIPT_FILE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_FILE")" 2>/dev/null && pwd -P)"

source "$SCRIPT_DIR/lib/vnx_resolve_root.sh"
vnx_resolve_project_root "$SCRIPT_FILE"   # exports VNX_PROJECT_ROOT
vnx_resolve_data_dir                       # exports VNX_DATA_DIR

ROOT="$VNX_PROJECT_ROOT"
# Unset VNX_PROJECT_ROOT immediately — the resolver exports it as a side-effect,
# but leaving it set leaks into pytest child processes and breaks test isolation
# (tests use monkeypatch but don't clean VNX_PROJECT_ROOT).
unset VNX_PROJECT_ROOT

cd "$ROOT"

# ─── Env ──────────────────────────────────────────────────────────────────────
export VNX_HOME="$ROOT"
export VNX_DATA_DIR="${VNX_DATA_DIR:-$ROOT/.vnx-data}"
export VNX_PROVENANCE_ENFORCEMENT=0
export VNX_PROVENANCE_LEGACY_ACCEPTED=1
export VNX_SLUG_ENFORCEMENT=0

# Python / pytest: prefer venv if available
if [ -x "$ROOT/.venv/bin/python3" ]; then
    PY="$ROOT/.venv/bin/python3"
else
    PY="python3"
fi
if [ -x "$ROOT/.venv/bin/pytest" ]; then
    PYTEST="$ROOT/.venv/bin/pytest"
else
    PYTEST="pytest"
fi

# ─── Flags ────────────────────────────────────────────────────────────────────
BASE_REF="origin/main"
WITH_NPM=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-ref)
            BASE_REF="${2:-origin/main}"
            shift 2
            ;;
        --with-npm)
            WITH_NPM=1
            shift
            ;;
        --help|-h)
            sed -n '2,/^[^#]/p' "$SCRIPT_FILE" | grep '^#' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "[local-ci] Unknown flag: $1" >&2
            exit 1
            ;;
    esac
done

HEAD_REF="$(git rev-parse --abbrev-ref HEAD)"

# ─── Gate runner ──────────────────────────────────────────────────────────────
declare -a NAMES RESULTS

run() {
    local name="$1"; shift
    local log="/tmp/lci_${name// /_}.log"
    echo "──────────────────────────────────────────────"
    echo "▶ $name"
    if "$@" > "$log" 2>&1; then
        echo "  [ok] $name"
        NAMES+=("$name"); RESULTS+=("ok")
    else
        local rc=$?
        echo "  [x] $name (rc=$rc) — tail:"
        tail -8 "$log" | sed 's/^/    /'
        NAMES+=("$name"); RESULTS+=("FAIL")
    fi
}

# ─── Gates ────────────────────────────────────────────────────────────────────

# 1. Legacy path gate (mirrors both profile-a and profile-b job steps)
legacy_gate() {
    local matches
    matches=$(rg -n '\.vnx-data/state/' "$ROOT/scripts" \
        --glob '!**/archived*/**' \
        --glob '!**/archive*/**' \
        --glob '!**/*.DEPRECATED' \
        --glob '!**/test_dashboard_sync.py' \
        --glob '!**/intelligence_export.py' \
        --glob '!**/intelligence_import.py' \
        --glob '!**/vnx_install.py' \
        --glob '!**/commands/merge_preflight.sh' \
        --glob '!**/migrate_to_central_vnx.py' \
        --glob '!**/migrate_dry_run.py' \
        || true)
    if [ -n "$matches" ]; then
        echo "$matches"
        echo "Legacy path literals detected. Use VNX_STATE_DIR via vnx_paths."
        return 1
    fi
}
run "legacy-path-gate" legacy_gate

# 2. Profile A — doctor
run "profileA-doctor" bash "$ROOT/scripts/vnx_doctor.sh"

# 3. Profile A — state_integrity (skipped if checksum not present, mirrors CI)
profile_a_integrity() {
    local state_file="$VNX_DATA_DIR/state/progress_state.yaml"
    local chk_file="$VNX_DATA_DIR/state/progress_state.yaml.sha256"
    if [ -f "$state_file" ] && [ -f "$chk_file" ]; then
        "$PY" "$ROOT/scripts/state_integrity.py" verify "$state_file"
    else
        echo "[skip] progress_state checksum not available"
    fi
}
run "profileA-state-integrity" profile_a_integrity

# 4. Profile A — core pytest files
run "profileA-tests" "$PYTEST" -q \
    "$ROOT/tests/test_cli_json_output.py" \
    "$ROOT/tests/test_validate_template_tokens.py" \
    "$ROOT/tests/test_receipt_ci_guard.py" \
    -p no:cacheprovider \
    --basetemp=/tmp/vnx_pytest_safe

# 5. Trace token — bash check
run "trace-token-check" bash "$ROOT/scripts/ci_trace_token_check.sh" "$BASE_REF"

# 6. Trace token — Python tests
run "trace-token-tests" "$PY" "$ROOT/tests/test_trace_token_validator.py"

# 7. Slug-match gate
slug_gate() {
    "$PY" "$ROOT/scripts/check_ci_slug_match.py" \
        --base-ref "$BASE_REF" \
        --branch-name "$HEAD_REF"
}
run "slug-match-gate" slug_gate

# 8. Slug-match unit tests
run "slug-match-tests" "$PYTEST" -q \
    "$ROOT/tests/test_ci_slug_match_gate.py" \
    -p no:cacheprovider \
    --basetemp=/tmp/vnx_pytest_safe

# 9. Profile C — path-resolution
run "profileC-path-resolution" "$PYTEST" -q \
    "$ROOT/tests/test_path_resolution_regression.py" \
    -p no:cacheprovider \
    --basetemp=/tmp/vnx_pytest_safe

# 10. Profile C — docs-command
run "profileC-docs-command" "$PYTEST" -q \
    "$ROOT/tests/test_docs_command_validation.py" \
    -p no:cacheprovider \
    --basetemp=/tmp/vnx_pytest_safe

# 11. Profile C — quickstart
run "profileC-quickstart" "$PYTEST" -q \
    "$ROOT/tests/test_quickstart_validation.py" \
    -p no:cacheprovider \
    --basetemp=/tmp/vnx_pytest_safe

# 12. Profile C — verify_closure
run "profileC-verify-closure" "$PY" "$ROOT/scripts/verify_closure.py"

# 13. Profile B — snapshot integration (mirrors vnx-ci.yml lines 277-299)
profile_b_snapshot() {
    local SNAPSHOT_ROOT
    SNAPSHOT_ROOT="$(mktemp -d /tmp/vnx-safe-tests.XXXXXX)"
    local SNAPSHOT_REPO="$SNAPSHOT_ROOT/repo"
    # shellcheck disable=SC2064
    trap "rm -rf '$SNAPSHOT_ROOT'" RETURN

    mkdir -p "$SNAPSHOT_REPO/.claude/vnx-system"
    rsync -a --exclude '__pycache__' "$ROOT/scripts/" "$SNAPSHOT_REPO/.claude/vnx-system/scripts/"
    rsync -a --exclude '__pycache__' "$ROOT/tests/"   "$SNAPSHOT_REPO/.claude/vnx-system/tests/"
    if [ -d "$ROOT/dispatches" ]; then
        rsync -a --exclude '__pycache__' "$ROOT/dispatches/" "$SNAPSHOT_REPO/.claude/vnx-system/dispatches/"
    else
        mkdir -p "$SNAPSHOT_REPO/.claude/vnx-system/dispatches"
    fi
    mkdir -p "$SNAPSHOT_REPO/.claude/vnx-system/state" \
             "$SNAPSHOT_REPO/.claude/vnx-system/templates"
    printf '# Feature: Snapshot\n' > "$SNAPSHOT_REPO/FEATURE_PLAN.md"

    (
        cd "$SNAPSHOT_REPO"
        export VNX_HOME="$SNAPSHOT_REPO/.claude/vnx-system"
        export VNX_DATA_DIR="$SNAPSHOT_REPO/.claude/vnx-system"
        "$PY" .claude/vnx-system/tests/test_pr_recommendation_integration.py
    )
}
run "profileB-snapshot-integration" profile_b_snapshot

# 14. lint-patterns (silent-except + atomic-write) on diff vs base
lint_patterns() {
    local changed
    changed=$(git diff --name-only "${BASE_REF}...HEAD" \
        | grep -E '^(scripts|dashboard)/.*\.py$' || true)
    if [ -z "$changed" ]; then
        echo "[skip] no py changes in scripts/ or dashboard/"
        return 0
    fi
    local added
    # shellcheck disable=SC2086
    added=$(git diff --unified=0 "${BASE_REF}...HEAD" -- $changed \
        | grep -E '^\+[^+]' | sed 's/^+//' || true)
    if [ -z "$added" ]; then
        echo "[skip] no added lines"
        return 0
    fi
    printf '%s\n' "$added" | "$PY" "$ROOT/scripts/ci_lint_patterns.py" --scan-stdin
}
run "lint-patterns" lint_patterns

# 15. dashboard-lint (npm run test:lint in dashboard/token-dashboard)
#     Runs when: --with-npm is set, OR the diff contains TS changes in the dashboard
dashboard_lint() {
    local dashboard_dir="$ROOT/dashboard/token-dashboard"
    if [ ! -d "$dashboard_dir" ]; then
        echo "[skip] dashboard/token-dashboard not found"
        return 0
    fi

    # Check if diff has TS changes in the dashboard dir
    local ts_changed
    ts_changed=$(git diff --name-only "${BASE_REF}...HEAD" \
        | grep -E '^dashboard/token-dashboard/.*\.(ts|tsx)$' || true)

    if [ "$WITH_NPM" -eq 0 ] && [ -z "$ts_changed" ]; then
        echo "[skip] no TS changes in dashboard/token-dashboard and --with-npm not set"
        echo "       Pass --with-npm to force dashboard-lint."
        return 0
    fi

    if [ ! -d "$dashboard_dir/node_modules" ]; then
        echo "[npm ci] installing dashboard dependencies..."
        (cd "$dashboard_dir" && npm ci)
    fi
    (cd "$dashboard_dir" && npm run test:lint)
}
run "dashboard-lint" dashboard_lint

# 16. ADR-003 — no anthropic SDK imports (mirrors anthropic-sdk-block.yml)
adr003() {
    if git grep -nE '^[[:space:]]*(import[[:space:]]+anthropic|from[[:space:]]+anthropic)' -- '*.py'; then
        echo "ADR-003 violation: anthropic SDK import found."
        return 1
    fi
}
run "adr003-no-anthropic-sdk" adr003

# ─── Summary matrix ───────────────────────────────────────────────────────────
echo
echo "═══════════════════ LOCAL CI SUMMARY ═══════════════════"
fail=0
for i in "${!NAMES[@]}"; do
    if [ "${RESULTS[$i]}" = "ok" ]; then
        printf "  ✅  %-38s %s\n" "${NAMES[$i]}" "ok"
    else
        printf "  ❌  %-38s %s\n" "${NAMES[$i]}" "FAIL"
        fail=$((fail + 1))
    fi
done
echo "─────────────────────────────────────────────────────────"
echo "  branch=$HEAD_REF"
echo "  base=$BASE_REF"
echo "  gates=${#NAMES[@]}  failed=$fail"
echo "─────────────────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then
    echo "  ALL LOCAL GATES GREEN"
else
    echo "  $fail gate(s) failed — see logs in /tmp/lci_*.log"
fi
exit "$fail"
