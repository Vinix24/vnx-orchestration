#!/usr/bin/env bash
# T0 gate enforcement wrapper — ensures request + execute + verify are atomic
# Usage: bash scripts/t0_gate_enforcement.sh --pr <num> --branch <branch> --review-stack <stack> --risk-class <risk> --changed-files <files>
set -euo pipefail

export VNX_STATE_DIR="${VNX_STATE_DIR:-.vnx-data/state}"
export VNX_DATA_DIR="${VNX_DATA_DIR:-.vnx-data}"
export VNX_CODEX_HEADLESS_ENABLED=1
export VNX_GEMINI_REVIEW_ENABLED=1

RESULT=$(python3 scripts/review_gate_manager.py request-and-execute "$@" 2>&1)
RC=$?

echo "$RESULT"

if [ $RC -ne 0 ]; then
    echo "GATE_ENFORCEMENT_FAILED: one or more required gates did not complete successfully" >&2
    exit 1
fi

# Verify artifacts exist
PR_NUM=$(echo "$@" | grep -oP '(?<=--pr )\d+')
for gate_type in gemini_review codex_gate; do
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
