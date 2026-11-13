#!/usr/bin/env bash
# impact_shadow_inputs.sh — the inputs of the impact shadow (ADR-074 besluit 2, 3, 7).
#
# Usage: bash scripts/ci/impact_shadow_inputs.sh OUT_DIR
#
# Writes OUT_DIR/base, OUT_DIR/candidate, OUT_DIR/paths.txt and OUT_DIR/selection.json.
# Shared by the Profile A shadow step and the shadow job, so both read one diff.
#
# The base follows the diff-range convention of the classifier step in
# .github/actions/profile-a-prelude/action.yml: the merge-base with the base ref
# on pull_request (the three-dot range), the parent commit on push. The resolved
# SHA is recorded, never the range text. A command that fails gives an empty
# base and an empty paths file. A selector that crashes or prints nothing removes
# selection.json and leaves the reason in OUT_DIR/selection-error.txt.
#
# Always exits 0: the shadow decides nothing and must not turn a check red.

set -u

OUT_DIR="${1:-}"
CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [ -z "$OUT_DIR" ] || ! mkdir -p "$OUT_DIR"; then
  printf 'impact_shadow_inputs: usage: impact_shadow_inputs.sh OUT_DIR\n' >&2
  exit 0
fi
rm -f "$OUT_DIR/base" "$OUT_DIR/candidate" "$OUT_DIR/paths.txt" "$OUT_DIR/selection.json" "$OUT_DIR/selection-error.txt"

candidate="$(git rev-parse HEAD 2>/dev/null)" || candidate=""
if [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ]; then
  base="$(git merge-base "origin/${GITHUB_BASE_REF:-}" HEAD 2>/dev/null)" || base=""
else
  base="$(git rev-parse 'HEAD^' 2>/dev/null)" || base=""
fi
printf '%s\n' "$base" > "$OUT_DIR/base"
printf '%s\n' "$candidate" > "$OUT_DIR/candidate"

: > "$OUT_DIR/paths.txt"
if [ -n "$base" ] && [ -n "$candidate" ]; then
  git diff --name-only "$base" "$candidate" > "$OUT_DIR/paths.txt" 2>/dev/null || : > "$OUT_DIR/paths.txt"
fi

rc=0
sel_err="$(python3 "$CI_DIR/impact_select.py" --paths-file "$OUT_DIR/paths.txt" 2>&1 > "$OUT_DIR/selection.json")" || rc=$?
if [ "$rc" -ne 0 ] || [ ! -s "$OUT_DIR/selection.json" ]; then
  rm -f "$OUT_DIR/selection.json"
  printf 'selector exit %s: %s\n' "$rc" "$(printf '%s' "$sel_err" | tail -n 1)" > "$OUT_DIR/selection-error.txt"
fi
exit 0
