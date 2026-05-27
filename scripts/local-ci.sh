#!/usr/bin/env bash
# local-ci.sh — run the local CI gate suite before pushing.
#
# A thin, dependency-free runner for the self-contained gates that can execute
# on a developer machine (or in a headless worker) without GitHub Actions. Each
# gate is independent; a failure in one does not abort the others, and the
# runner exits non-zero if ANY gate failed so the result is unambiguous.
#
# Registered gates:
#   adr-003-no-sdk   billing-safety: no Anthropic SDK imports anywhere in-tree
#   wheel-install    packaging acceptance: build wheel -> fresh venv -> install
#                    -> vnx --version / vnx doctor / VNX_HOME schema resolution
#                    (scripts/test_wheel_install.sh)
#
# Usage:
#   bash scripts/local-ci.sh            # run all gates
#   SKIP_WHEEL=1 bash scripts/local-ci.sh   # skip the slow wheel smoke
#
# Env overrides:
#   SMOKE_PYTHON   interpreter for the wheel smoke (forwarded), default python3
#   SKIP_WHEEL     when set to 1, skip the wheel-install gate (fast inner loop)
#
# Exit codes:
#   0 — every gate passed (or was skipped)
#   1 — one or more gates failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

log()  { printf '\033[94m[local-ci]\033[0m %s\n' "$*"; }
ok()   { printf '\033[92m[ pass ]\033[0m %s\n' "$*"; }
bad()  { printf '\033[91m[ fail ]\033[0m %s\n' "$*" >&2; }
skip() { printf '\033[93m[ skip ]\033[0m %s\n' "$*"; }

FAILED=()

# run_gate <name> <command...> — run a gate, capturing output; report status.
run_gate() {
    local name="$1"; shift
    log "running gate: $name"
    local out
    if out="$("$@" 2>&1)"; then
        ok "$name"
    else
        bad "$name (exit $?)"
        printf '%s\n' "$out" | sed 's/^/    /' >&2
        FAILED+=("$name")
    fi
}

# --- gate: ADR-003 no-SDK import guard (billing safety) --------------------
run_gate "adr-003-no-sdk" python3 "$REPO_ROOT/scripts/check_adr_003_no_sdk_imports.py"

# --- gate: wheel-install packaging smoke -----------------------------------
if [ "${SKIP_WHEEL:-0}" = "1" ]; then
    skip "wheel-install (SKIP_WHEEL=1)"
else
    run_gate "wheel-install" bash "$REPO_ROOT/scripts/test_wheel_install.sh"
fi

echo
if [ "${#FAILED[@]}" -gt 0 ]; then
    bad "local CI: ${#FAILED[@]} gate(s) failed: ${FAILED[*]}"
    exit 1
fi
ok "local CI: all gates passed"
