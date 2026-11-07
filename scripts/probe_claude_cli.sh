#!/usr/bin/env bash
# probe_claude_cli.sh: measure cell B of the agents-als-map track (point 5).
#
# Question: does `claude -p` WITHOUT --bare still authenticate on the own account
# (OAuth), or has --bare become the default and the subscription lane broken?
#
# Only the T0/operator runs this. A builder cannot start `claude -p` (the
# pretooluse_block_raw_claude_spawn.sh hook blocks it). The probe runs one
# non-interactive call in a throwaway probe tree under $TMPDIR with a throwaway
# data store, and prints one table row for docs/operations/CLAUDE_CLI_MEASUREMENTS.md:
#
#   | date | claude --version | ja / nee / niet gemeten: reason | sha256 of the probe output |
#
# Outcomes: ja (call succeeded, OAuth works), nee (call failed demanding an API key),
# niet gemeten (no claude, no version, timeout, or a failure that is not an API-key
# demand). Only a row with ja/nee AND a 64-hex sha256 counts as a measurement; the
# doctor check `claude-cli` WARNs on anything else.
#
# Usage: scripts/probe_claude_cli.sh [--append] [--doc <path>]
#   --append   append the row to the measurements doc (default: print only)
#   --doc      measurements doc (default: docs/operations/CLAUDE_CLI_MEASUREMENTS.md)
# Env: PROBE_TIMEOUT_SECONDS (default 120), PROBE_CLAUDE_BIN (default: claude)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOC="$REPO_ROOT/docs/operations/CLAUDE_CLI_MEASUREMENTS.md"
APPEND=0
CLAUDE_BIN="${PROBE_CLAUDE_BIN:-claude}"
TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-120}"
PROBE_DIR=""

die() { echo "probe_claude_cli: $*" >&2; exit 2; }

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --append) APPEND=1 ;;
      --doc) [ "$#" -ge 2 ] || die "--doc needs a path"; DOC="$2"; shift ;;
      *) die "unknown argument: $1" ;;
    esac
    shift
  done
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

cleanup() {
  case "$PROBE_DIR" in
    "${TMPDIR:-/tmp}"/*|/tmp/*|/var/folders/*) rm -rf -- "$PROBE_DIR" ;;
  esac
}

# The version string, or empty when claude is missing or prints no x.y.z.
read_version() {
  "$CLAUDE_BIN" --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true
}

# Run the call in the probe tree; the exit status lands in $1, the output in $2.
run_call() {
  local status_file="$1" out_file="$2"
  (
    cd "$PROBE_DIR/klant"
    export VNX_DATA_DIR="$PROBE_DIR/data" VNX_DATA_DIR_EXPLICIT=1
    set +e
    perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" \
      "$CLAUDE_BIN" -p "Reply with the single word OK." --output-format text \
      >"$out_file" 2>&1
    echo "$?" >"$status_file"
  )
}

# outcome <exit-status> <output-file>: prints "ja" / "nee" / "niet gemeten: <reason>".
classify() {
  local status="$1" out_file="$2"
  if [ "$status" = "0" ] && [ -s "$out_file" ]; then
    echo "ja"
  elif [ "$status" = "142" ]; then
    echo "niet gemeten: timeout after ${TIMEOUT_SECONDS}s"
  elif [ "$status" != "0" ] && grep -Eiq 'ANTHROPIC_API_KEY|api key' "$out_file"; then
    echo "nee"
  else
    echo "niet gemeten: exit ${status} without an API-key demand"
  fi
}

emit_row() {
  local version="$1" outcome="$2" sha="$3"
  printf '| %s | %s | %s | %s |\n' "$(date -u +%F)" "$version" "$outcome" "$sha"
}

main() {
  parse_args "$@"
  local version row
  version="$(read_version)"
  if [ -z "$version" ]; then
    row="$(emit_row "unknown" "niet gemeten: ${CLAUDE_BIN} --version gave no version" "-")"
  else
    PROBE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/probe-claude-cli.XXXXXX")"
    trap cleanup EXIT
    mkdir -p "$PROBE_DIR/klant" "$PROBE_DIR/data"
    run_call "$PROBE_DIR/status" "$PROBE_DIR/output"
    row="$(emit_row "$version" "$(classify "$(cat "$PROBE_DIR/status")" "$PROBE_DIR/output")" \
      "$(sha256_of "$PROBE_DIR/output")")"
  fi
  echo "$row"
  if [ "$APPEND" = "1" ]; then
    [ -f "$DOC" ] || die "measurements doc not found: $DOC"
    echo "$row" >>"$DOC"
  fi
}

main "$@"
