#!/usr/bin/env bash
# test_install_nightly_crons.sh — hermetic test for install_nightly_crons.sh idempotency.
# Does NOT touch the real crontab. Uses a fake crontab shim that reads/writes a temp file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

CRON_FILE="$TMPDIR/installed_cron"
CRON_SHIM="$TMPDIR/crontab_shim"

# --- Fake crontab shim ---
# crontab -l: prints contents of $CRON_FILE; exits 1 if empty (like real crontab -l)
# crontab - : reads stdin and writes to $CRON_FILE
cat > "$CRON_SHIM" << 'CRONSHIM'
#!/usr/bin/env bash
# $1 = CRON_FILE path, $2 = crontab operator (-l or -)
CRON_FILE="$1"

if [ "$#" -lt 2 ]; then
    echo "Usage: crontab [-l | -]" >&2
    exit 1
fi

case "${2:-}" in
    -l)
        if [ -s "$CRON_FILE" ]; then
            cat "$CRON_FILE"
        else
            exit 1
        fi
        ;;
    -)
        cat > "$CRON_FILE"
        ;;
    *)
        echo "Usage: crontab [-l | -]" >&2
        exit 1
        ;;
esac
CRONSHIM
chmod +x "$CRON_SHIM"

# Override PATH so our fake crontab is found first.
# We wrap via a helper script that passes $CRON_FILE as arg to the shim.
CRON_WRAPPER="$TMPDIR/crontab_wrapper"
cat > "$CRON_WRAPPER" << WRAPPER
#!/usr/bin/env bash
exec "$CRON_SHIM" "$CRON_FILE" "\$@"
WRAPPER
chmod +x "$CRON_WRAPPER"

# Symlink crontab -> wrapper so the script's `crontab` invocations use our shim.
ln -sf "$CRON_WRAPPER" "$TMPDIR/crontab"

export PATH="$TMPDIR:$PATH"

# --- Test runner ---
FAILURES=0

assert_contains() {
    local file="$1" pattern="$2" label="$3"
    if grep -qF "$pattern" "$file"; then
        printf '  PASS: %s\n' "$label"
    else
        printf '  FAIL: %s — pattern "%s" not found in %s\n' "$label" "$pattern" "$file"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_count() {
    local file="$1" pattern="$2" expected="$3" label="$4"
    local actual
    actual=$(grep -cF "$pattern" "$file" || true)
    if [ "$actual" -eq "$expected" ]; then
        printf '  PASS: %s (count=%d)\n' "$label" "$actual"
    else
        printf '  FAIL: %s — expected %d occurrences, got %d\n' "$label" "$expected" "$actual"
        FAILURES=$((FAILURES + 1))
    fi
}

printf '=== Hermetic test: install_nightly_crons.sh ===\n\n'

# ── First run ───────────────────────────────────────────────────────────────────
printf 'First run:\n'
VNX_HOME="$REPO_ROOT" bash "$REPO_ROOT/scripts/install_nightly_crons.sh" > /dev/null 2>&1 || {
    printf '  FAIL: install_nightly_crons.sh exited non-zero on first run\n'
    FAILURES=$((FAILURES + 1))
}

printf '\nChecking all three entries are present:\n'
assert_contains "$CRON_FILE" "compact_state.py" "compact_state entry present"
assert_contains "$CRON_FILE" "rotate_shadow_ledger.sh" "shadow rotation entry present"
assert_contains "$CRON_FILE" "nightly_intelligence_pipeline.sh" "intel pipeline entry present"

# ── Second run (idempotency) ────────────────────────────────────────────────────
printf '\nSecond run (idempotency):\n'
VNX_HOME="$REPO_ROOT" bash "$REPO_ROOT/scripts/install_nightly_crons.sh" > /dev/null 2>&1 || {
    printf '  FAIL: install_nightly_crons.sh exited non-zero on second run\n'
    FAILURES=$((FAILURES + 1))
}

printf '\nChecking idempotency — intel entry appears exactly once:\n'
assert_count "$CRON_FILE" "compact_state.py" 1 "compact_state appears exactly once"
assert_count "$CRON_FILE" "nightly_intelligence_pipeline.sh" 1 "intel pipeline appears exactly once"

# ── Result ───────────────────────────────────────────────────────────────────────
printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf '=== PASS ===\n'
    exit 0
else
    printf '=== FAIL (%d assertion(s) failed) ===\n' "$FAILURES"
    printf '\nContents of installed_cron for debugging:\n'
    cat "$CRON_FILE"
    exit 1
fi
