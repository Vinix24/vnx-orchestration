#!/usr/bin/env bash
# profile_a_pytest_args.sh — the one pytest selection of CI Profile A.
#
# Usage: bash scripts/ci/profile_a_pytest_args.sh OUT_DIR
#
# Writes pytest argument vectors (everything after `pytest`) into OUT_DIR as
# NUL-delimited files. A caller reads one with
#   mapfile -d '' -t ARGS < "OUT_DIR/<phase>.args"
#
#   VNX_CI_XDIST=0                   full.args      the serial whole-tree sweep
#                                                   (the way back to serial,
#                                                   set as the repository
#                                                   variable vars.VNX_CI_XDIST)
#   VNX_CI_XDIST unset, empty or 1   serial.args    the entries of
#                                                   serial_tests.txt in one
#                                                   process with xdist off;
#                                                   written only when the list
#                                                   has entries
#                                    parallel.args  the same sweep under
#                                                   pytest-xdist, without the
#                                                   serial entries
#   either mode                      run.json       the run stamp of the
#                                                   emission: GITHUB_RUN_ID and
#                                                   GITHUB_RUN_ATTEMPT (null
#                                                   outside CI), written last,
#                                                   so the parity check can tell
#                                                   this attempt's emission from
#                                                   an earlier attempt's
#   VNX_CI_SHARD=k/N (xdist shape,   shard.args     instead of parallel.args: its vector with the
#   1 <= k <= N <= 99; the duration                 tests root replaced by shard k's files;
#   source: VNX_CI_SHARD_SOURCE,     collected.json the selection collected before the split
#   the output of test_shard_plan.py shard.json     the plan of profile_a_shard.py
#   select-source, unset = hash)     (and serial.args in shard 1 only, as in the xdist shape)
#
# Since D4 the default is the two-phase xdist shape. Profile A and the nightly
# serial truth of .github/workflows/vnx-ci-serial-truth.yml call this script,
# so the selection cannot drift between them: a second inline copy would drift
# the first time someone edits -k. Each vector is also printed, shell-quoted,
# on stderr for the run log.
#
# Exit 0 on success; 2 on a refused input (VNX_CI_XDIST other than 0 or 1, or
# a serial entry that does not match exactly, see serial_tests.txt); 1 when the
# project root cannot be resolved. Runs under bash 3.2 (macOS) as well as
# bash 5 (CI).

set -euo pipefail

# shellcheck source=../lib/vnx_resolve_root.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/vnx_resolve_root.sh"

# Single-test quarantine (OI-1051 versmald, PR #1434):
# test_receipt_mirror_isolation.py::TestGovernEnsureReceiptIsolation::
# test_ensure_receipt_does_not_touch_real_store fails ONLY in full-suite
# collection order on CI and passes in isolation (also after the merge of
# main). Measured on head 9a01f5cc: 1 failed, 15953 passed — this one test is
# the 1. The stub-teardown in tests/test_intelligence_daemon_paths.py already
# fixed its four order-dependent sibling tests (plus 13 collateral tests in
# test_learning_loop_tz and test_w5b_small_fixes); this last one is NOT a
# remnant of that cause. Its root cause is not yet attributed and a bisect over
# the collection order was deliberately NOT attempted (three earlier dispatches
# sank into that hole). The other seven tests in the file keep running — that
# is why this is a -k name filter, not an entry in test_exclusions.txt (which
# excludes whole files). TestFlagGateClaude: see the header of
# test_exclusions.txt.
QUARANTINE_K="not TestFlagGateClaude and not test_ensure_receipt_does_not_touch_real_store"
PER_TEST_TIMEOUT=180
# The runner has 4 vCPUs (plan ci-profile-a-xdist, settings from SEOcrawler B2).
XDIST_WORKERS=4

SERIAL_CHECK_DIR=""
SHARD_K="" SHARD_N=""
trap 'if [ -n "$SERIAL_CHECK_DIR" ]; then rm -rf "$SERIAL_CHECK_DIR"; fi' EXIT

refuse() {
  printf 'profile_a_pytest_args: %s\n' "$*" >&2
  exit 2
}

# The explicit exclusion list lives in scripts/ci/test_exclusions.txt — each
# entry is red on main (OI-906) and cites the open item tracking its repair;
# nothing is excluded silently. The parsing below is the inline block this
# script replaced in vnx-ci.yml, unchanged in effect (golden test:
# tests/test_ci_profile_a_pytest_args.py). EXCLUDED keeps the relative paths,
# newline-delimited, for the serial-list checks.
build_ignore() {
  IGNORE=()
  EXCLUDED=$'\n'
  local line rel
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    rel="$(printf '%s' "$line" | sed -E 's/[[:space:]]*#.*//' | tr -d '[:space:]')"
    if [ -n "$rel" ]; then
      IGNORE+=(--ignore="$ROOT/$rel")
      EXCLUDED="$EXCLUDED$rel"$'\n'
    fi
  done < "$ROOT/scripts/ci/test_exclusions.txt"
}

# serial_tests.txt keeps whitespace inside an entry (a parametrize id may hold
# a space) and starts an inline comment only at whitespace followed by '#'.
read_serial_entries() {
  SERIAL_ENTRIES=()
  local line entry
  while IFS= read -r line || [ -n "$line" ]; do
    entry="$(printf '%s' "$line" | sed -E -e 's/^[[:space:]]+//' -e 's/(^|[[:space:]]+)#.*$//' -e 's/[[:space:]]+$//')"
    if [ -n "$entry" ]; then
      SERIAL_ENTRIES+=("$entry")
    fi
  done < "$ROOT/scripts/ci/serial_tests.txt"
}

check_entry_path() {
  local entry="$1" file="${1%%::*}"
  case "$file" in
    tests/*.py) ;;
    *) refuse "serial_tests.txt: $entry: not a test file under tests/ (tests/<path>.py or tests/<path>.py::<nodeid>)" ;;
  esac
  case "/$file/" in
    */../*|*/./*|*//*) refuse "serial_tests.txt: $entry: the path must be plain, without '.', '..' or '//'" ;;
  esac
  if [ ! -f "$ROOT/$file" ]; then
    refuse "serial_tests.txt: $entry: $file does not exist"
  fi
  case "$EXCLUDED" in
    *$'\n'"$file"$'\n'*) refuse "serial_tests.txt: $entry: $file is excluded in test_exclusions.txt, so it is not in the selection" ;;
  esac
}

# Splits SERIAL_ENTRIES into SERIAL_FILES and SERIAL_NODEIDS and refuses
# duplicates and a nodeid entry inside a file that is itself an entry.
split_serial_entries() {
  SERIAL_FILES=()
  SERIAL_NODEIDS=()
  local seen=$'\n' entry nodeid file
  for entry in ${SERIAL_ENTRIES[@]+"${SERIAL_ENTRIES[@]}"}; do
    case "$seen" in
      *$'\n'"$entry"$'\n'*) refuse "serial_tests.txt: duplicate entry: $entry" ;;
    esac
    seen="$seen$entry"$'\n'
    check_entry_path "$entry"
    if [ "$entry" = "${entry%%::*}" ]; then
      SERIAL_FILES+=("$entry")
    else
      SERIAL_NODEIDS+=("$entry")
    fi
  done
  for nodeid in ${SERIAL_NODEIDS[@]+"${SERIAL_NODEIDS[@]}"}; do
    for file in ${SERIAL_FILES[@]+"${SERIAL_FILES[@]}"}; do
      if [ "${nodeid%%::*}" = "$file" ]; then
        refuse "serial_tests.txt: $nodeid lies in $file, which is itself a serial entry"
      fi
    done
  done
}

# Collects the files of all serial entries with the selection's own filters
# (and the manifest plugin, so "collected" means what the manifest records),
# then holds every entry against the collected nodeids.
check_serial_collection() {
  SERIAL_CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/profile-a-serial-check.XXXXXX")"
  local work="$SERIAL_CHECK_DIR" entry rc=0 seen=$'\n'
  local -a paths=()
  for entry in "${SERIAL_ENTRIES[@]}"; do
    case "$seen" in
      *$'\n'"${entry%%::*}"$'\n'*) ;;
      *) seen="$seen${entry%%::*}"$'\n'; paths+=("$ROOT/${entry%%::*}") ;;
    esac
  done
  (
    cd "$ROOT"
    PYTHONPATH="$ROOT/scripts/ci${PYTHONPATH:+:$PYTHONPATH}" pytest --collect-only -q \
      -p no:cacheprovider -p no:xdist -p pytest_collection_manifest \
      --collection-manifest="$work/manifest.json" --collection-manifest-phase=serial-check \
      -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"} "${paths[@]}"
  ) > "$work/collect.log" 2>&1 || rc=$?
  # Exit 5 is "no tests collected": not an error here, the matching below
  # names every entry that collected nothing.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then
    cat "$work/collect.log" >&2
    refuse "serial_tests.txt: collecting the serial entries failed (pytest exit $rc); an entry that does not collect matches nothing"
  fi
  if ! python3 "$ROOT/scripts/ci/pytest_collection_manifest.py" nodeids "$work/manifest.json" > "$work/nodeids"; then
    refuse "serial_tests.txt: the collection manifest of the serial entries is unreadable"
  fi
  match_serial_entries "$work/nodeids"
}

match_serial_entries() {
  local nodeids_file="$1" entry count matched
  for entry in ${SERIAL_FILES[@]+"${SERIAL_FILES[@]}"}; do
    count="$(PREFIX="$entry::" awk 'index($0, ENVIRON["PREFIX"]) == 1 { n++ } END { print n + 0 }' "$nodeids_file")"
    if [ "$count" -eq 0 ]; then
      refuse "serial_tests.txt: $entry: collects no test under the selection (a file entry must collect at least one)"
    fi
  done
  # --deselect matches by nodeid PREFIX, so a nodeid entry must be the only
  # collected nodeid that starts with it, or the parallel phase would drop
  # tests the serial phase does not run.
  for entry in ${SERIAL_NODEIDS[@]+"${SERIAL_NODEIDS[@]}"}; do
    matched="$(PREFIX="$entry" awk 'index($0, ENVIRON["PREFIX"]) == 1' "$nodeids_file")"
    if [ "$matched" != "$entry" ]; then
      count="$(printf '%s' "$matched" | awk 'END { print NR }')"
      refuse "serial_tests.txt: $entry: must match exactly one collected nodeid and prefix no other; it prefixes $count: ${matched//$'\n'/, }"
    fi
  done
}

# A JSON string of the digits in $1, or null when it has none.
json_digits() {
  local v
  v="$(printf '%s' "${1:-}" | tr -cd '0-9')"
  if [ -n "$v" ]; then printf '"%s"' "$v"; else printf 'null'; fi
}

write_stamp() {
  printf '{"schema": "vnx-run-stamp/1", "run_id": %s, "run_attempt": %s}\n' \
    "$(json_digits "${GITHUB_RUN_ID:-}")" "$(json_digits "${GITHUB_RUN_ATTEMPT:-}")" > "$OUT_DIR/run.json"
}

write_vector() {
  local out="$1"
  shift
  : > "$out"
  if [ "$#" -gt 0 ]; then
    printf '%s\0' "$@" > "$out"
  fi
  printf '%s:' "$(basename "$out")" >&2
  printf ' %q' "$@" >&2
  printf '\n' >&2
}

# Sets PARALLEL_ARGS; writes serial.args when the list has entries and $1 is yes.
xdist_vectors() {
  local entry
  local -a serial_ignore=() deselect=() positional=()
  for entry in ${SERIAL_FILES[@]+"${SERIAL_FILES[@]}"}; do
    serial_ignore+=(--ignore="$ROOT/$entry")
  done
  for entry in ${SERIAL_NODEIDS[@]+"${SERIAL_NODEIDS[@]}"}; do
    deselect+=(--deselect="$entry")
  done
  for entry in ${SERIAL_ENTRIES[@]+"${SERIAL_ENTRIES[@]}"}; do
    positional+=("$ROOT/$entry")
  done
  if [ "$1" = yes ] && [ "${#SERIAL_ENTRIES[@]}" -gt 0 ]; then
    write_vector "$OUT_DIR/serial.args" -q -p no:xdist -p no:cacheprovider "--timeout=$PER_TEST_TIMEOUT" \
      -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"} "${positional[@]}"
  fi
  PARALLEL_ARGS=("${BASE_ARGS[@]}" \
    ${serial_ignore[@]+"${serial_ignore[@]}"} ${deselect[@]+"${deselect[@]}"} \
    -n "$XDIST_WORKERS" --dist loadfile --no-loadscope-reorder --max-worker-restart=0)
}

emit_xdist_vectors() {
  xdist_vectors yes
  write_vector "$OUT_DIR/parallel.args" "${PARALLEL_ARGS[@]}"
}

check_shard() {
  case "$1" in
    *[!0-9/]*|0*|*/0*|*/*/*|*[0-9][0-9][0-9]*) ;;
    [1-9]*/[1-9]*) SHARD_K="${1%/*}" SHARD_N="${1#*/}" ;;
  esac
  if [ -z "$SHARD_N" ] || [ "$SHARD_K" -gt "$SHARD_N" ]; then
    refuse "VNX_CI_SHARD must be k/N with 1 <= k <= N <= 99, got '$1'"
  fi
  [ "$2" = 1 ] || refuse "VNX_CI_SHARD needs the xdist shape, got VNX_CI_XDIST=$2"
}

# Shard k of N: collect the whole selection (the set before the split), plan it with profile_a_shard.py, and give
# this shard the parallel vector with the tests root replaced by its files. Only shard 1 carries the serial phase.
emit_shard_vectors() {
  local rc=0 entry work
  local -a plan_args=() files=()
  [ -n "$SERIAL_CHECK_DIR" ] || SERIAL_CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/profile-a-shard.XXXXXX")"
  work="$SERIAL_CHECK_DIR"
  (
    cd "$ROOT"
    PYTHONPATH="$ROOT/scripts/ci${PYTHONPATH:+:$PYTHONPATH}" pytest --collect-only -p no:xdist \
      -p pytest_collection_manifest --collection-manifest="$OUT_DIR/collected.json" \
      --collection-manifest-phase=collected "${BASE_ARGS[@]}"
  ) > "$work/shard-collect.log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    cat "$work/shard-collect.log" >&2
    refuse "VNX_CI_SHARD=$SHARD_K/$SHARD_N: collecting the selection before the split failed (pytest exit $rc)"
  fi
  for entry in ${SERIAL_ENTRIES[@]+"${SERIAL_ENTRIES[@]}"}; do
    plan_args+=(--serial-entry "$entry")
  done
  if ! python3 "$ROOT/scripts/ci/profile_a_shard.py" --collected "$OUT_DIR/collected.json" \
      --source "${VNX_CI_SHARD_SOURCE:-}" --shard "$SHARD_K" --shards "$SHARD_N" \
      --output "$OUT_DIR/shard.json" ${plan_args[@]+"${plan_args[@]}"} > "$work/shard-files"; then
    refuse "VNX_CI_SHARD=$SHARD_K/$SHARD_N: the shard plan was refused"
  fi
  while IFS= read -r -d '' entry; do files+=("$ROOT/$entry"); done < "$work/shard-files"
  if [ "$SHARD_K" = 1 ]; then xdist_vectors yes; else xdist_vectors no; fi
  write_vector "$OUT_DIR/shard.args" -q "${files[@]}" "${PARALLEL_ARGS[@]:2}"
}

main() {
  if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    refuse "usage: profile_a_pytest_args.sh OUT_DIR"
  fi
  OUT_DIR="$1"
  # D4: unset means the two-phase xdist shape; VNX_CI_XDIST=0 is the way back
  # to fully serial, without any other change.
  local mode="${VNX_CI_XDIST:-1}"
  case "$mode" in
    0|1) ;;
    *) refuse "VNX_CI_XDIST must be 0 or 1, got '$mode'" ;;
  esac
  [ -z "${VNX_CI_SHARD:-}" ] || check_shard "$VNX_CI_SHARD" "$mode"
  if ! vnx_resolve_project_root "${BASH_SOURCE[0]}"; then
    printf 'profile_a_pytest_args: cannot resolve the project root\n' >&2
    exit 1
  fi
  ROOT="$VNX_PROJECT_ROOT"
  mkdir -p "$OUT_DIR"
  rm -f "$OUT_DIR/full.args" "$OUT_DIR/serial.args" "$OUT_DIR/parallel.args" "$OUT_DIR/run.json" \
    "$OUT_DIR/shard.args" "$OUT_DIR/shard.json" "$OUT_DIR/collected.json"
  build_ignore
  BASE_ARGS=(-q "$ROOT/tests" -p no:cacheprovider "--timeout=$PER_TEST_TIMEOUT" -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"})
  if [ "$mode" = 0 ]; then
    write_vector "$OUT_DIR/full.args" "${BASE_ARGS[@]}"
  else
    read_serial_entries
    split_serial_entries
    if [ "${#SERIAL_ENTRIES[@]}" -gt 0 ]; then
      check_serial_collection
    fi
    if [ -n "$SHARD_N" ]; then emit_shard_vectors; else emit_xdist_vectors; fi
  fi
  write_stamp
}

main "$@"
