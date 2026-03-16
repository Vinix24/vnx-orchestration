#!/bin/bash
# VNX Doctor - Path hygiene checks for Phase P (P1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"

PATTERN='\.claude/vnx-system|/Users/'
SCRIPTS_DIR="$VNX_HOME/scripts"
TEMPLATES_DIR="$VNX_HOME/templates"

if command -v rg >/dev/null 2>&1; then
  MATCHES=$(rg -n "$PATTERN" \
    "$SCRIPTS_DIR" "$TEMPLATES_DIR" \
    --glob '**/*.sh' \
    --glob '**/*.py' \
    --glob "$TEMPLATES_DIR/**/*.md" \
    --glob '!**/archived*' \
    --glob '!**/archive*' \
    --glob '!**/vnx_doctor.sh' \
    --glob '!**/vnx_worktree_setup.sh' \
    --glob '!**/vnx_worktree_merge_data.sh' \
    --glob '!**/*.deprecated' \
    --glob '!**/*.log' || true)
else
  # Fallback to grep if ripgrep is unavailable
  MATCHES=$(grep -R -n -E "$PATTERN" \
    "$SCRIPTS_DIR" "$TEMPLATES_DIR" \
    --include='*.sh' \
    --include='*.py' \
    --include='*.md' \
    --exclude-dir='archived*' \
    --exclude-dir='archive*' \
    --exclude='vnx_doctor.sh' \
    --exclude='vnx_worktree_setup.sh' \
    --exclude='vnx_worktree_merge_data.sh' \
    --exclude='*.deprecated' \
    --exclude='*.log' || true)
fi

if [ -n "$MATCHES" ]; then
  echo "[vnx doctor] FAILED: Found forbidden path references:"
  echo "$MATCHES"
  exit 1
fi

echo "[vnx doctor] OK: No forbidden path references in scripts/templates."
exit 0
