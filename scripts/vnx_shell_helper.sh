#!/usr/bin/env bash
# VNX Shell Helper Installer
# Adds a vnx() shell function to ~/.bashrc or ~/.zshrc that resolves
# the project-local vnx binary in both .vnx/ (primary) and
# .claude/vnx-system/ (legacy) layouts.
#
# Usage:
#   vnx install-shell-helper          # Auto-detect shell, install
#   vnx install-shell-helper --print  # Print helper function only
#   vnx install-shell-helper --shell bash  # Force specific shell
set -euo pipefail

VNX_HELPER_MARKER="# >>> VNX shell helper >>>"
VNX_HELPER_END="# <<< VNX shell helper <<<"

print_helper() {
  cat <<'HELPER'
# >>> VNX shell helper >>>
# Resolves project-local vnx binary. Searches up from CWD.
vnx() {
  local dir="$PWD"
  local vnx_bin=""
  while [ "$dir" != "/" ]; do
    if [ -x "$dir/.vnx/bin/vnx" ]; then
      vnx_bin="$dir/.vnx/bin/vnx"
      break
    elif [ -x "$dir/.claude/vnx-system/bin/vnx" ]; then
      vnx_bin="$dir/.claude/vnx-system/bin/vnx"
      break
    fi
    dir="$(dirname "$dir")"
  done
  if [ -z "$vnx_bin" ]; then
    echo "vnx: no .vnx/bin/vnx or .claude/vnx-system/bin/vnx found in parent directories" >&2
    return 1
  fi
  "$vnx_bin" "$@"
}
# <<< VNX shell helper <<<
HELPER
}

install_helper() {
  local shell_name="$1"
  local rc_file=""

  case "$shell_name" in
    zsh)  rc_file="$HOME/.zshrc" ;;
    bash) rc_file="$HOME/.bashrc" ;;
    *)
      echo "ERROR: Unsupported shell: $shell_name" >&2
      echo "Supported: bash, zsh" >&2
      return 1
      ;;
  esac

  if [ ! -f "$rc_file" ]; then
    touch "$rc_file"
  fi

  # Check if already installed
  if grep -q "$VNX_HELPER_MARKER" "$rc_file" 2>/dev/null; then
    echo "[vnx] Shell helper already installed in $rc_file"
    echo "[vnx] To update, remove the existing block and re-run."
    return 0
  fi

  # Append helper
  echo "" >> "$rc_file"
  print_helper >> "$rc_file"

  echo "[vnx] Shell helper installed in $rc_file"
  echo "[vnx] Restart your shell or run: source $rc_file"
}

uninstall_helper() {
  local shell_name="$1"
  local rc_file=""

  case "$shell_name" in
    zsh)  rc_file="$HOME/.zshrc" ;;
    bash) rc_file="$HOME/.bashrc" ;;
    *)    return 1 ;;
  esac

  if [ ! -f "$rc_file" ]; then
    return 0
  fi

  if ! grep -q "$VNX_HELPER_MARKER" "$rc_file" 2>/dev/null; then
    echo "[vnx] No shell helper found in $rc_file"
    return 0
  fi

  # Remove the helper block (marker to end marker)
  local tmpfile
  tmpfile="$(mktemp)"
  awk "/$VNX_HELPER_MARKER/{skip=1} /$VNX_HELPER_END/{skip=0; next} !skip" "$rc_file" > "$tmpfile"
  mv "$tmpfile" "$rc_file"

  echo "[vnx] Shell helper removed from $rc_file"
}

# ── CLI ──────────────────────────────────────────────────────────────────
main() {
  local action="install"
  local shell_override=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --print)      action="print"; shift ;;
      --uninstall)  action="uninstall"; shift ;;
      --shell)      shell_override="$2"; shift 2 ;;
      --shell=*)    shell_override="${1#*=}"; shift ;;
      -h|--help)
        echo "Usage: vnx install-shell-helper [options]"
        echo ""
        echo "Options:"
        echo "  --print       Print helper function to stdout (no install)"
        echo "  --uninstall   Remove helper from shell RC"
        echo "  --shell <sh>  Force shell type (bash|zsh, default: auto-detect)"
        echo "  -h, --help    Show this help"
        return 0
        ;;
      *) shift ;;
    esac
  done

  if [ "$action" = "print" ]; then
    print_helper
    return 0
  fi

  # Auto-detect shell
  local target_shell="${shell_override:-}"
  if [ -z "$target_shell" ]; then
    case "${SHELL:-/bin/bash}" in
      */zsh)  target_shell="zsh" ;;
      *)      target_shell="bash" ;;
    esac
  fi

  if [ "$action" = "uninstall" ]; then
    uninstall_helper "$target_shell"
  else
    install_helper "$target_shell"
  fi
}

main "$@"
