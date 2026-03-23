#!/bin/bash
# VNX Doctor - Path hygiene checks for Phase P (P1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"

PATTERN='\.claude/vnx-system|/Users/|\.nvm/versions/node/v[0-9]'
SCRIPTS_DIR="$VNX_HOME/scripts"
TEMPLATES_DIR="$VNX_HOME/templates"
BIN_DIR="$VNX_HOME/bin"

if command -v rg >/dev/null 2>&1; then
  MATCHES=$(rg -n "$PATTERN" \
    "$SCRIPTS_DIR" "$TEMPLATES_DIR" "$BIN_DIR" \
    --glob '**/*.sh' \
    --glob '**/*.py' \
    --glob "$TEMPLATES_DIR/**/*.md" \
    --glob '!**/archived*' \
    --glob '!**/archive*' \
    --glob '!**/vnx_doctor.sh' \
    --glob '!**/vnx_worktree_setup.sh' \
    --glob '!**/vnx_worktree_merge_data.sh' \
    --glob '!**/vnx_shell_helper.sh' \
    --glob '!**/commands/registry.sh' \
    --glob '!**/intelligence_export.py' \
    --glob '!**/intelligence_import.py' \
    --glob '!**/commands/merge_preflight.sh' \
    --glob '!**/*.deprecated' \
    --glob '!**/*.log' || true)
  # Also check bin/vnx (no extension, so glob won't match it)
  if [ -f "$BIN_DIR/vnx" ]; then
    local_matches=$(rg -n "$PATTERN" "$BIN_DIR/vnx" || true)
    [ -n "$local_matches" ] && MATCHES="${MATCHES:+$MATCHES
}$local_matches"
  fi
else
  # Fallback to grep if ripgrep is unavailable
  MATCHES=$(grep -R -n -E "$PATTERN" \
    "$SCRIPTS_DIR" "$TEMPLATES_DIR" "$BIN_DIR" \
    --include='*.sh' \
    --include='*.py' \
    --include='*.md' \
    --exclude-dir='archived*' \
    --exclude-dir='archive*' \
    --exclude='vnx_doctor.sh' \
    --exclude='vnx_worktree_setup.sh' \
    --exclude='vnx_worktree_merge_data.sh' \
    --exclude='vnx_shell_helper.sh' \
    --exclude='registry.sh' \
    --exclude='intelligence_export.py' \
    --exclude='intelligence_import.py' \
    --exclude='merge_preflight.sh' \
    --exclude='*.deprecated' \
    --exclude='*.log' || true)
  # Also check bin/vnx
  if [ -f "$BIN_DIR/vnx" ]; then
    local_matches=$(grep -n -E "$PATTERN" "$BIN_DIR/vnx" || true)
    [ -n "$local_matches" ] && MATCHES="${MATCHES:+$MATCHES
}$local_matches"
  fi
fi

if [ -n "$MATCHES" ]; then
  echo "[vnx doctor] FAILED: Found forbidden path references:"
  echo "$MATCHES"
  exit 1
fi

echo "[vnx doctor] OK: No forbidden path references in scripts/templates."
exit 0
