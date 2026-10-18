#!/usr/bin/env bash
# profile_a_run_phase.sh — run one pytest phase of CI Profile A with its evidence.
#
# Usage: bash scripts/ci/profile_a_run_phase.sh PHASE EVIDENCE_DIR
#
# PHASE is full, serial or parallel. Reads the argument vector that
# scripts/ci/profile_a_pytest_args.sh wrote to EVIDENCE_DIR/args/PHASE.args and
# runs `pytest` with it plus the evidence arguments, so Profile A and the xdist
# shadow write their evidence the same way:
#
#   junit-PHASE.xml              JUnit XML of the run
#   manifest-PHASE.json          collection manifest (scripts/ci/pytest_collection_manifest.py)
#   exit-PHASE                   pytest's exit code (absent when the step was killed)
#   duration-PHASE-s             wall time of the pytest process in seconds
#   result-PHASE.json            exit code and wall time with the run stamp
#                                (GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT; null outside
#                                CI), so the parity check can tell this attempt's
#                                result from an earlier attempt's
#   mem-total-kb                 MemTotal of the runner        (Linux only)
#   mem-baseline-used-kb-PHASE   MemTotal - MemAvailable before the run
#   mem-peak-used-kb-PHASE       the highest MemTotal - MemAvailable seen during
#                                the run, sampled every 2 seconds; this covers
#                                every process on the runner, so it includes
#                                the baseline
#
# Exits with pytest's exit code, so the calling step fails exactly when the
# suite fails. Runs under bash 3.2 as well as bash 5.

set -euo pipefail

usage() {
  printf 'usage: profile_a_run_phase.sh full|serial|parallel EVIDENCE_DIR\n' >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
PHASE="$1"
EVIDENCE="$2"
case "$PHASE" in
  full|serial|parallel) ;;
  *) usage ;;
esac
ARGS_FILE="$EVIDENCE/args/$PHASE.args"
if [ ! -f "$ARGS_FILE" ]; then
  printf 'profile_a_run_phase: %s is missing; run scripts/ci/profile_a_pytest_args.sh first\n' "$ARGS_FILE" >&2
  exit 2
fi
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MEM_SAMPLER_PID=""

mem_used_kb() {
  awk '/^MemTotal:/ { t = $2 } /^MemAvailable:/ { a = $2 } END { print t - a }' /proc/meminfo
}

start_memory_sampler() {
  if [ ! -r /proc/meminfo ]; then
    return 0
  fi
  awk '/^MemTotal:/ { print $2 }' /proc/meminfo > "$EVIDENCE/mem-total-kb"
  mem_used_kb > "$EVIDENCE/mem-baseline-used-kb-$PHASE"
  (
    peak=0
    while :; do
      used="$(mem_used_kb)"
      if [ "$used" -gt "$peak" ]; then
        peak="$used"
        printf '%s\n' "$peak" > "$EVIDENCE/mem-peak-used-kb-$PHASE"
      fi
      sleep 2
    done
  ) &
  MEM_SAMPLER_PID=$!
}

stop_memory_sampler() {
  if [ -n "$MEM_SAMPLER_PID" ]; then
    kill "$MEM_SAMPLER_PID" 2>/dev/null || true
    wait "$MEM_SAMPLER_PID" 2>/dev/null || true
    MEM_SAMPLER_PID=""
  fi
}
trap stop_memory_sampler EXIT

PYTEST_ARGS=()
while IFS= read -r -d '' arg; do
  PYTEST_ARGS+=("$arg")
done < "$ARGS_FILE"
if [ "${#PYTEST_ARGS[@]}" -eq 0 ]; then
  printf 'profile_a_run_phase: %s is empty\n' "$ARGS_FILE" >&2
  exit 2
fi

# A JSON string of the digits in $1, or null when it has none.
json_digits() {
  local v
  v="$(printf '%s' "${1:-}" | tr -cd '0-9')"
  if [ -n "$v" ]; then printf '"%s"' "$v"; else printf 'null'; fi
}

write_result() {
  local tmp="$EVIDENCE/.result-$PHASE.json.tmp"
  printf '{"schema": "vnx-phase-result/1", "phase": "%s", "run_id": %s, "run_attempt": %s, "exit": %s, "duration_s": %s}\n' \
    "$PHASE" "$(json_digits "${GITHUB_RUN_ID:-}")" "$(json_digits "${GITHUB_RUN_ATTEMPT:-}")" "$1" "$2" > "$tmp"
  mv -f "$tmp" "$EVIDENCE/result-$PHASE.json"
}

rm -f "$EVIDENCE/exit-$PHASE" "$EVIDENCE/duration-$PHASE-s" "$EVIDENCE/result-$PHASE.json"
start_memory_sampler
started="$(date +%s)"
rc=0
PYTHONPATH="$PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" pytest "${PYTEST_ARGS[@]}" \
  --junitxml="$EVIDENCE/junit-$PHASE.xml" \
  -p pytest_collection_manifest \
  --collection-manifest="$EVIDENCE/manifest-$PHASE.json" \
  --collection-manifest-phase="$PHASE" || rc=$?
duration="$(( $(date +%s) - started ))"
printf '%s\n' "$duration" > "$EVIDENCE/duration-$PHASE-s"
printf '%s\n' "$rc" > "$EVIDENCE/exit-$PHASE"
write_result "$rc" "$duration"
stop_memory_sampler
exit "$rc"
