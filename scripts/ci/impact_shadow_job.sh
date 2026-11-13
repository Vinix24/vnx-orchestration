#!/usr/bin/env bash
# impact_shadow_job.sh — the shadow job that runs only S (ADR-074 besluit 3 and 7c).
#
# Usage: bash scripts/ci/impact_shadow_job.sh OUT_DIR
#
# Builds the inputs, and when the selection is reduced runs the files of S - X
# with the Profile A selection (profile_a_pytest_args.sh: ignores, -k quarantine,
# per-test timeout, -n) through profile_a_run_phase.sh: the serial entries that
# are in S in the serial phase, the rest in the parallel phase, so the wall clock
# compares to Profile A. No selection, scope full, an empty selection or no
# reduction means no pytest: the saving is zero by definition, and the record
# names the reason (no_selection, expand:<reason>, empty_selection, not_reduced).
#
# Writes OUT_DIR/job.json (impact_shadow_job/1, via impact_shadow.py job). A red
# or crashed phase is recorded, never raised. Always exits 0: the job can never
# turn a check red (it is also continue-on-error in the workflow).

set -u

OUT_DIR="${1:-}"
CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd "$CI_DIR/../.." && pwd -P)"
SHADOW="$CI_DIR/impact_shadow.py"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
failed=""
wall=""
exit_args=()

if [ -z "$OUT_DIR" ]; then
  printf 'impact shadow job: no record (usage: impact_shadow_job.sh OUT_DIR)\n' | tee -a "$SUMMARY"
  exit 0
fi
EVIDENCE="$OUT_DIR/evidence"
mkdir -p "$EVIDENCE/args" || exit 0

finish() {
  local line
  if line="$(python3 "$SHADOW" job --output "$OUT_DIR/job.json" \
    --run-id "${GITHUB_RUN_ID:-}" --run-attempt "${GITHUB_RUN_ATTEMPT:-}" \
    --base "$(cat "$OUT_DIR/base" 2>/dev/null)" --candidate "$(cat "$OUT_DIR/candidate" 2>/dev/null)" \
    --selection "$OUT_DIR/selection.json" --evidence "$EVIDENCE" \
    ${wall:+--wall-seconds "$wall"} ${failed:+--failed-reason "$failed"} \
    ${exit_args[@]+"${exit_args[@]}"} 2> "$OUT_DIR/job.err")"; then
    printf '%s\n' "$line" | tee -a "$SUMMARY"
  else
    printf 'impact shadow job: no record (impact_shadow.py job: %s)\n' "$(tail -n 1 "$OUT_DIR/job.err" 2>/dev/null)" | tee -a "$SUMMARY"
  fi
  exit 0
}

bash "$CI_DIR/impact_shadow_inputs.sh" "$OUT_DIR"
plan="$(python3 "$SHADOW" plan --selection "$OUT_DIR/selection.json" 2> "$OUT_DIR/plan.err")" || { failed="plan_failed"; finish; }
[ "$plan" = "run" ] || finish

bash "$CI_DIR/profile_a_pytest_args.sh" "$EVIDENCE/args" > "$OUT_DIR/args.log" 2>&1 || { failed="args_failed"; finish; }
phases="$(python3 "$SHADOW" shape --selection "$OUT_DIR/selection.json" --args-dir "$EVIDENCE/args" --root "$ROOT" 2>> "$OUT_DIR/args.log")" || { failed="shape_failed"; finish; }
[ -n "$phases" ] || { failed="empty_selection"; finish; }

started="$(date +%s)"
for phase in $phases; do
  rc=0
  bash "$CI_DIR/profile_a_run_phase.sh" "$phase" "$EVIDENCE" || rc=$?
  exit_args+=(--phase-exit "$phase=$rc")
done
wall="$(( $(date +%s) - started ))"
finish
