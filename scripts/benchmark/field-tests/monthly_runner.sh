#!/usr/bin/env bash
# monthly_runner.sh — full field-tests run for monthly cadence
# Runs all tiers × all lanes × N=3 (T1+T2) / N=2 (T3), writes results + diffs against prior month.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
RESULTS_BASE="$HERE/results"

cd "$REPO_ROOT"

STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
THIS_RUN="$RESULTS_BASE/$STAMP"
mkdir -p "$THIS_RUN"

echo "[monthly_runner] Starting full field-tests run → $THIS_RUN"

python3 "$HERE/runners/run_field_tests.py" \
  --parallel 6 \
  --results-dir "$THIS_RUN" \
  2>&1 | tee "$THIS_RUN/runner.log"

PRIOR_RUN="$(find "$RESULTS_BASE" -maxdepth 1 -mindepth 1 -type d \
    ! -path "$THIS_RUN" | sort | tail -1 || true)"

if [ -n "${PRIOR_RUN:-}" ] && [ -f "$PRIOR_RUN/summary.md" ]; then
  echo "[monthly_runner] Diffing against prior: $PRIOR_RUN"
  diff -u "$PRIOR_RUN/summary.md" "$THIS_RUN/summary.md" > "$THIS_RUN/diff-vs-prior.patch" || true
  echo "  diff written: $THIS_RUN/diff-vs-prior.patch"
else
  echo "[monthly_runner] No prior run found; skipping diff"
fi

echo "[monthly_runner] Done. Review:"
echo "  $THIS_RUN/summary.md"
echo "  $THIS_RUN/per-lane.md"
echo "  $THIS_RUN/methodology.md"
