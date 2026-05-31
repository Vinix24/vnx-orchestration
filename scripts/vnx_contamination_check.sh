#!/usr/bin/env bash
# VNX contamination check — detect pre-fix runtime state inside a central install.
#
# Wave 4 PR-4. Before the install-central path-resolver fix (PR-WAVE4-1..3), a
# mis-resolved PROJECT_ROOT could write project runtime state — .vnx-data,
# .claude/settings.json, .vnx-intelligence — *into* the immutable central code
# tree (~/.vnx-system/versions/<v>/). After the fix, new invocations write to
# the correct project dir, but any pre-fix contamination silently persists.
#
# This sweeps the central install for such contamination and prints actionable
# cleanup instructions. It NEVER deletes anything — removal is an explicit
# operator decision (receipts written into the central tree may need to be
# copied into the owning project first).
#
# Usage:
#   vnx_contamination_check.sh                       # scan ~/.vnx-system/versions/*
#   vnx_contamination_check.sh --install-root DIR    # scan DIR/versions/*
#   vnx_contamination_check.sh --version-dir DIR     # scan exactly DIR
#   vnx_contamination_check.sh --quiet               # suppress the "clean" message
#
# Resolution order for the scan root (when neither flag is given):
#   1. $VNX_CENTRAL_ROOT
#   2. $HOME/.vnx-system
#
# Exit codes:
#   0  no contamination found (or nothing to scan)
#   1  contamination found (warnings printed to stderr)
#   2  usage error

set -u

# Runtime directories that must never live inside the immutable central tree.
BLOCKED_ENTRIES=(".vnx-data" ".claude" ".vnx-intelligence")

_cc_out()  { printf '%s\n' "$*"; }
_cc_warn() { printf '%s\n' "$*" >&2; }

usage() {
  cat <<'USAGE'
Usage: vnx_contamination_check.sh [--install-root DIR | --version-dir DIR] [--quiet]

Detects pre-fix runtime contamination (.vnx-data, .claude, .vnx-intelligence)
inside a central VNX install. Warns with cleanup instructions; never deletes.

Exit 0 = clean, 1 = contamination found, 2 = usage error.
USAGE
}

main() {
  local install_root=""
  local version_dir=""
  local quiet=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --install-root)
        install_root="${2:-}"
        [ -n "$install_root" ] || { _cc_warn "[contamination-check] --install-root requires a path"; return 2; }
        shift 2
        ;;
      --version-dir)
        version_dir="${2:-}"
        [ -n "$version_dir" ] || { _cc_warn "[contamination-check] --version-dir requires a path"; return 2; }
        shift 2
        ;;
      --quiet)
        quiet=1
        shift
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        _cc_warn "[contamination-check] Unknown option: $1"
        usage >&2
        return 2
        ;;
    esac
  done

  # Build the list of version dirs to scan.
  local -a scan_dirs=()
  if [ -n "$version_dir" ]; then
    scan_dirs+=("$version_dir")
  else
    [ -n "$install_root" ] || install_root="${VNX_CENTRAL_ROOT:-$HOME/.vnx-system}"
    local versions_dir="$install_root/versions"
    if [ -d "$versions_dir" ]; then
      local d
      for d in "$versions_dir"/*/; do
        # nullglob-safe: a non-matching glob yields the literal pattern, which
        # is not a directory and is skipped here.
        [ -d "$d" ] || continue
        scan_dirs+=("${d%/}")
      done
    fi
  fi

  if [ "${#scan_dirs[@]}" -eq 0 ]; then
    [ "$quiet" -eq 1 ] || _cc_out "[contamination-check] No central install version dirs to scan."
    return 0
  fi

  local found=0
  local vdir entry path
  for vdir in "${scan_dirs[@]}"; do
    [ -d "$vdir" ] || continue
    for entry in "${BLOCKED_ENTRIES[@]}"; do
      path="$vdir/$entry"
      if [ -e "$path" ]; then
        found=1
        _cc_warn "[contamination-check] WARN: runtime state inside central install:"
        _cc_warn "  $path"
        _cc_warn "  Cleanup (after copying any needed receipts into your project): rm -rf '$path'"
      fi
    done
  done

  if [ "$found" -eq 1 ]; then
    _cc_warn "[contamination-check] Pre-fix contamination detected. Review and remove the paths above manually."
    return 1
  fi

  [ "$quiet" -eq 1 ] || _cc_out "[contamination-check] Clean: no runtime state inside central install."
  return 0
}

main "$@"
