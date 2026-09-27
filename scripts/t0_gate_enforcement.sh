#!/usr/bin/env bash
# T0 gate enforcement wrapper — ensures request + execute + verify are atomic
# Usage: bash scripts/t0_gate_enforcement.sh --pr <num> --branch <branch> --review-stack <stack> --risk-class <risk> --changed-files <files>
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/vnx_resolve_root.sh"
vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
vnx_resolve_data_dir
vnx_resolve_state_dir
vnx_resolve_dispatch_dir
export VNX_CODEX_HEADLESS_ENABLED=1

RC=0
RESULT=$(python3 scripts/review_gate_manager.py request-and-execute "$@" 2>&1) || RC=$?

echo "$RESULT"

if [ $RC -ne 0 ]; then
    echo "GATE_ENFORCEMENT_FAILED: one or more required gates did not complete successfully" >&2
    exit 1
fi

# Verify artifacts exist for the gates that actually ran. The requested stack
# names seats, and review_gate_manager may fill a seat with another gate (a
# takeover down VNX_REVIEW_GATE_TAKEOVER_CHAIN) or request a shared reader once,
# so the gates verified are the ones request-and-execute reported, not the raw
# --review-stack. A requested seat that no reported gate ran, took over or
# recorded as chain-exhausted is still MISSING_ARTIFACT (fail-closed).
printf '%s\n' "$RESULT" | python3 "$(dirname "${BASH_SOURCE[0]}")/lib/gate_enforcement_verify.py" \
    --state-dir "$VNX_STATE_DIR" -- "$@" || exit 1
