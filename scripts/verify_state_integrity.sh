#!/bin/bash
# Verify checksums for critical VNX state files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"

CHECKER="$SCRIPT_DIR/state_integrity.py"

if [ ! -x "$CHECKER" ]; then
  echo "❌ Missing state_integrity.py at $CHECKER"
  exit 1
fi

STATE_DIR_CANDIDATES=(
  "$VNX_STATE_DIR"
  "$VNX_HOME/state"
)

resolve_target() {
  local filename="$1"
  for dir in "${STATE_DIR_CANDIDATES[@]}"; do
    if [ -f "$dir/$filename" ]; then
      echo "$dir/$filename"
      return 0
    fi
  done
  echo "${VNX_STATE_DIR}/$filename"
  return 1
}

TARGETS=(
  # `|| true`: resolve_target returns 1 on a miss by design (it still echoes the
  # fallback path), and an unguarded array substitution would abort here under
  # errexit before the missing-file reporting below can run.
  "$(resolve_target pr_queue.json || true)"
  "$(resolve_target progress_state.yaml || true)"
)

missing=0
for target in "${TARGETS[@]}"; do
  if [ ! -f "$target" ]; then
    echo "❌ Missing state file: $target"
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  exit 1
fi

failed=0
for target in "${TARGETS[@]}"; do
  if python3 "$CHECKER" verify "$target" >/dev/null 2>&1; then
    echo "✅ Verified: $target"
  else
    echo "❌ Checksum mismatch: $target"
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  exit 2
fi

echo "✅ State integrity verified"
exit 0
