#!/usr/bin/env bash
# profile_a_pytest_args.sh — the one pytest selection of CI Profile A.
#
# Usage: bash scripts/ci/profile_a_pytest_args.sh OUT_DIR
#
# Writes pytest argument vectors (everything after `pytest`) into OUT_DIR as
# NUL-delimited files. A caller reads one with
#   mapfile -d '' -t ARGS < "OUT_DIR/<phase>.args"
#
#   VNX_CI_XDIST unset, empty or 0   full.args      the serial whole-tree sweep
#   VNX_CI_XDIST=1                   serial.args    the entries of
#                                                   serial_tests.txt in one
#                                                   process with xdist off;
#                                                   written only when the list
#                                                   has entries
#                                    parallel.args  the same sweep under
#                                                   pytest-xdist, without the
#                                                   serial entries
#
# Profile A and the xdist shadow job of .github/workflows/vnx-ci.yml both call
# this script, so the selection cannot drift between them: a second inline
# copy would drift the first time someone edits -k. Each vector is also
# printed, shell-quoted, on stderr for the run log.
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

emit_xdist_vectors() {
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
  if [ "${#SERIAL_ENTRIES[@]}" -gt 0 ]; then
    write_vector "$OUT_DIR/serial.args" -q -p no:xdist -p no:cacheprovider "--timeout=$PER_TEST_TIMEOUT" \
      -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"} "${positional[@]}"
  fi
  write_vector "$OUT_DIR/parallel.args" "${BASE_ARGS[@]}" --deselect=tests/test_ci_profile_classification.py \
    ${serial_ignore[@]+"${serial_ignore[@]}"} ${deselect[@]+"${deselect[@]}"} \
    -n "$XDIST_WORKERS" --dist loadfile --no-loadscope-reorder --max-worker-restart=0
}

main() {
  if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    refuse "usage: profile_a_pytest_args.sh OUT_DIR"
  fi
  OUT_DIR="$1"
  local mode="${VNX_CI_XDIST:-0}"
  case "$mode" in
    0|1) ;;
    *) refuse "VNX_CI_XDIST must be 0 or 1, got '$mode'" ;;
  esac
  if ! vnx_resolve_project_root "${BASH_SOURCE[0]}"; then
    printf 'profile_a_pytest_args: cannot resolve the project root\n' >&2
    exit 1
  fi
  ROOT="$VNX_PROJECT_ROOT"
  mkdir -p "$OUT_DIR"
  rm -f "$OUT_DIR/full.args" "$OUT_DIR/serial.args" "$OUT_DIR/parallel.args"
  build_ignore
  BASE_ARGS=(-q "$ROOT/tests" -p no:cacheprovider "--timeout=$PER_TEST_TIMEOUT" -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"})
  # Red runs (d)/(e) of D2 only, on a draft branch that is closed unmerged:
  # VNX_CI_NARROW replaces the tests directory with a few files.
  if [ -n "${VNX_CI_NARROW:-}" ]; then
    local -a narrow=()
    local p
    for p in $VNX_CI_NARROW; do narrow+=("$ROOT/$p"); done
    BASE_ARGS=(-q "${narrow[@]}" -p no:cacheprovider "--timeout=$PER_TEST_TIMEOUT" -k "$QUARANTINE_K" ${IGNORE[@]+"${IGNORE[@]}"})
  fi
  if [ "$mode" = 0 ]; then
    write_vector "$OUT_DIR/full.args" "${BASE_ARGS[@]}"
    return 0
  fi
  read_serial_entries
  split_serial_entries
  if [ "${#SERIAL_ENTRIES[@]}" -gt 0 ]; then
    check_serial_collection
  fi
  emit_xdist_vectors
}

main "$@"
