#!/usr/bin/env bash
# VNX CI: Trace Token Validation
# Scans commit messages in a PR or branch for trace token compliance.
#
# CLI-agnostic durable backstop (A-R4, A-R6, G-R8).
# This script does NOT depend on any specific AI CLI.
#
# Usage:
#   ci_trace_token_check.sh [base_ref]
#
# Environment:
#   VNX_PROVENANCE_ENFORCEMENT: "0" = warn, "1" = block (default: from env)
#   VNX_PROVENANCE_LEGACY_ACCEPTED: "1" = accept legacy (default: "1")
#   GITHUB_BASE_REF: used if base_ref not provided (GitHub Actions)
#
# Exit codes:
#   0 = all commits valid (or shadow mode)
#   1 = invalid commits found (enforcement mode only)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATOR="$SCRIPT_DIR/lib/trace_token_validator.py"
ENFORCEMENT="${VNX_PROVENANCE_ENFORCEMENT:-0}"

# ── Determine base ref ──────────────────────────────────────────────
BASE_REF="${1:-${GITHUB_BASE_REF:-main}}"

# Ensure base ref exists
if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
    if git rev-parse --verify "origin/$BASE_REF" >/dev/null 2>&1; then
        BASE_REF="origin/$BASE_REF"
    else
        echo "[VNX-CI] WARNING: Base ref '$BASE_REF' not found. Skipping check." >&2
        exit 0
    fi
fi

# ── Find commits to check ───────────────────────────────────────────
MERGE_BASE="$(git merge-base "$BASE_REF" HEAD 2>/dev/null)" || MERGE_BASE="$BASE_REF"
COMMITS="$(git log "$MERGE_BASE..HEAD" --format="%H" 2>/dev/null)" || true

if [ -z "$COMMITS" ]; then
    echo "[VNX-CI] No new commits to check."
    exit 0
fi

TOTAL=0
VALID=0
INVALID=0
LEGACY=0
FAILED_SHAS=""

echo "══════════════════════════════════════════════════════════"
echo " VNX Trace Token Check"
echo " Base: $BASE_REF  Enforcement: $([ "$ENFORCEMENT" = "1" ] && echo "ENFORCED" || echo "shadow")"
echo "══════════════════════════════════════════════════════════"
echo ""

while IFS= read -r sha; do
    [ -z "$sha" ] && continue
    TOTAL=$((TOTAL + 1))

    SUBJECT="$(git log -1 --format="%s" "$sha")"
    BODY="$(git log -1 --format="%B" "$sha")"

    # Use Python validator if available
    if [ -f "$VALIDATOR" ]; then
        RESULT="$(echo "$BODY" | python3 "$VALIDATOR" validate - 2>/dev/null)" || RESULT=""

        if [ -n "$RESULT" ]; then
            IS_VALID="$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('valid', False))" 2>/dev/null)" || IS_VALID="unknown"
            FORMAT="$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('format') or 'none')" 2>/dev/null)" || FORMAT="unknown"
        else
            IS_VALID="unknown"
            FORMAT="unknown"
        fi
    else
        # Inline fallback
        IS_VALID="False"
        FORMAT="none"

        if echo "$BODY" | grep -qE '^Dispatch-ID:\s+\S+'; then
            IS_VALID="True"
            FORMAT="preferred"
        elif [ "${VNX_PROVENANCE_LEGACY_ACCEPTED:-1}" = "1" ]; then
            if echo "$BODY" | grep -qE 'dispatch:\S+|\\bPR-[0-9]+\\b|\\bFP-[A-Z]\\b'; then
                IS_VALID="True"
                FORMAT="legacy"
            fi
        fi
    fi

    # Report
    SHORT_SHA="${sha:0:8}"
    if [ "$IS_VALID" = "True" ]; then
        VALID=$((VALID + 1))
        ICON="✓"
        if [ "$FORMAT" != "preferred" ] && [ "$FORMAT" != "none" ]; then
            LEGACY=$((LEGACY + 1))
            ICON="~"
        fi
    elif [ "$IS_VALID" = "unknown" ]; then
        # Validator error — don't count as invalid
        VALID=$((VALID + 1))
        ICON="?"
    else
        INVALID=$((INVALID + 1))
        ICON="✗"
        FAILED_SHAS="${FAILED_SHAS}${sha}\n"
    fi

    printf "  %s %s %s [%s]\n" "$ICON" "$SHORT_SHA" "$SUBJECT" "$FORMAT"
done <<< "$COMMITS"

echo ""
echo "──────────────────────────────────────────────────────────"
printf "  Total: %d | Valid: %d | Legacy: %d | Missing: %d\n" "$TOTAL" "$VALID" "$LEGACY" "$INVALID"
echo "──────────────────────────────────────────────────────────"

if [ "$INVALID" -gt 0 ]; then
    echo ""
    echo "Commits missing trace tokens:"
    echo -e "$FAILED_SHAS" | while IFS= read -r fsha; do
        [ -z "$fsha" ] && continue
        echo "  - $fsha $(git log -1 --format='%s' "$fsha")"
    done
    echo ""
    echo "Fix: Add 'Dispatch-ID: <dispatch-id>' to the commit body."
    echo ""

    if [ "$ENFORCEMENT" = "1" ]; then
        echo "RESULT: FAIL (enforcement mode)"
        exit 1
    else
        echo "RESULT: WARN (shadow mode — not blocking)"
        exit 0
    fi
fi

echo ""
echo "RESULT: PASS"
exit 0
