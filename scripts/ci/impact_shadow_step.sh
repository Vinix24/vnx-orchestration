#!/usr/bin/env bash
# impact_shadow_step.sh — the impact shadow step of CI Profile A.
#
# Usage: bash scripts/ci/impact_shadow_step.sh EVIDENCE_DIR OUT_DIR
#
# Builds the inputs, then the impact_shadow/1 record against the Profile A
# evidence into OUT_DIR/record.json, and appends the summary line to
# GITHUB_STEP_SUMMARY. A missing selection, a selector crash or an
# impact_shadow.py crash writes one line "impact shadow: no record (<reason>)"
# to the summary and stops. Exits 0 on every path: the step can never turn a
# check red (it also runs under continue-on-error in the workflow).

set -u

EVIDENCE="${1:-}"
OUT_DIR="${2:-}"
CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

no_record() {
  printf 'impact shadow: no record (%s)\n' "$1" | tee -a "$SUMMARY"
  exit 0
}

if [ -z "$EVIDENCE" ] || [ -z "$OUT_DIR" ]; then
  no_record "usage: impact_shadow_step.sh EVIDENCE_DIR OUT_DIR"
fi
bash "$CI_DIR/impact_shadow_inputs.sh" "$OUT_DIR" || no_record "inputs script exit $?"

# The sharded Profile A (C3) keeps each shard's evidence under shard-k/;
# impact_backtest.shard_view copies it flat into the phase names
# impact_shadow.py reads. The single-job layout is read as it is.
if compgen -G "$EVIDENCE/shard-*/" > /dev/null; then
  FLAT="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/impact-shadow-evidence.XXXXXX")" || no_record "no temp dir"
  PYTHONPATH="$CI_DIR/../lib${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
    'import sys; from impact_backtest import shard_view; shard_view(sys.argv[1], sys.argv[2])' \
    "$EVIDENCE" "$FLAT" 2> "$OUT_DIR/shards.err" \
    || no_record "shard evidence: $(tail -n 1 "$OUT_DIR/shards.err" 2>/dev/null)"
  EVIDENCE="$FLAT"
fi
if [ ! -s "$OUT_DIR/selection.json" ]; then
  no_record "$(cat "$OUT_DIR/selection-error.txt" 2>/dev/null || printf 'no selection')"
fi

rc=0
line="$(python3 "$CI_DIR/impact_shadow.py" \
  --selection "$OUT_DIR/selection.json" --evidence "$EVIDENCE" \
  --run-id "${GITHUB_RUN_ID:-}" --run-attempt "${GITHUB_RUN_ATTEMPT:-}" \
  --base "$(cat "$OUT_DIR/base")" --candidate "$(cat "$OUT_DIR/candidate")" \
  --paths-file "$OUT_DIR/paths.txt" --output "$OUT_DIR/record.json" 2> "$OUT_DIR/record.err")" || rc=$?
if [ "$rc" -ne 0 ]; then
  no_record "impact_shadow.py exit $rc: $(tail -n 1 "$OUT_DIR/record.err" 2>/dev/null)"
fi
printf '%s\n' "$line" | tee -a "$SUMMARY"
exit 0
