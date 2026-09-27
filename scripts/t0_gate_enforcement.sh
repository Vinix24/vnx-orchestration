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

# Verify artifacts exist
PR_NUM=$(echo "$@" | sed -nE 's/.*--pr ([0-9]+).*/\1/p')
# Verify every seat of the stack that was requested: the --review-stack
# argument when given, otherwise the stack review_gate_manager resolves from
# VNX_DEFAULT_REVIEW_STACK. A hardcoded list here drifts from that config and
# lets a required seat pass unverified.
REVIEW_STACK=$(echo "$@" | sed -nE 's/.*--review-stack[ =]([^ ]+).*/\1/p')
if [ -z "$REVIEW_STACK" ]; then
    REVIEW_STACK=$(cd scripts && python3 -c "import review_gate_manager as m; print(','.join(m.DEFAULT_REVIEW_STACK))")
fi
if [ -z "$REVIEW_STACK" ]; then
    echo "GATE_ENFORCEMENT_FAILED: review stack resolved empty; nothing to verify" >&2
    exit 1
fi
for gate_type in ${REVIEW_STACK//,/ }; do
    REQUEST_FILE="$VNX_STATE_DIR/review_gates/requests/pr-${PR_NUM}-${gate_type}.json"
    RESULT_FILE="$VNX_STATE_DIR/review_gates/results/pr-${PR_NUM}-${gate_type}.json"

    if [ ! -f "$REQUEST_FILE" ]; then
        echo "MISSING_ARTIFACT: $REQUEST_FILE" >&2
        exit 1
    fi
    if [ ! -f "$RESULT_FILE" ]; then
        echo "MISSING_ARTIFACT: $RESULT_FILE" >&2
        exit 1
    fi

    # Check result status
    STATUS=$(python3 -c "import json; print(json.load(open('$RESULT_FILE')).get('status','unknown'))")
    if [ "$STATUS" != "completed" ] && [ "$STATUS" != "passed" ]; then
        echo "GATE_NOT_COMPLETED: $gate_type status=$STATUS" >&2
        # Don't exit — report but let T0 decide
    fi
done

echo "GATE_ENFORCEMENT_COMPLETE: all artifacts verified"
