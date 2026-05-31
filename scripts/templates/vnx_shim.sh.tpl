#!/usr/bin/env bash
# VNX project-pin shim — reads .vnx-version from project root (cwd traversal)
set -euo pipefail

VNX_SYSTEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Traverse up from cwd to find .vnx-version
find_version_pin() {
  local dir="$PWD"
  while [ "$dir" != "/" ]; do
    if [ -f "${dir}/.vnx-version" ]; then
      cat "${dir}/.vnx-version"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  echo ""
}

pin="$(find_version_pin | head -1 | tr -d '\n[:space:]')"

if [ -n "$pin" ]; then
  if ! [[ "$pin" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[vnx-shim] ERROR: invalid pin '${pin}' in .vnx-version (must match [A-Za-z0-9._-]+)" >&2
    exit 78  # EX_CONFIG
  fi
  version_dir="${VNX_SYSTEM_DIR}/versions/${pin}"
  if [ ! -d "$version_dir" ]; then
    echo "[vnx-shim] [x] Pinned version ${pin} not installed at ${version_dir}" >&2
    echo "[vnx-shim] Run: bash ${VNX_SYSTEM_DIR}/../install-central.sh --version ${pin}" >&2
    exit 1
  fi
  if command -v realpath >/dev/null 2>&1; then
    resolved=$(realpath "$version_dir" 2>/dev/null) || { echo "[vnx-shim] ERROR: cannot resolve version_dir: ${version_dir}" >&2; exit 78; }
    versions_root=$(realpath "${VNX_SYSTEM_DIR}/versions")
    if [[ "$resolved" != "$versions_root"/* ]]; then
      echo "[vnx-shim] ERROR: pin '${pin}' escapes versions root" >&2; exit 78
    fi
  fi
  export VNX_HOME="$version_dir"
else
  if [ ! -e "${VNX_SYSTEM_DIR}/current" ]; then
    echo "[vnx-shim] [x] No .vnx-version pin found and no current install at ${VNX_SYSTEM_DIR}/current" >&2
    exit 1
  fi
  export VNX_HOME="${VNX_SYSTEM_DIR}/current"
fi

# ── Project root detection (central install) ────────────────────────────────
# The inner resolver (scripts/lib/vnx_paths.*) prefers VNX_PROJECT_ROOT when it
# is set. Clear any inherited value first, so a stale export left in the
# operator's shell (set for a different project) cannot survive a `cd` and leak
# the wrong project root.
unset VNX_PROJECT_ROOT

# Walk up from $PWD looking for a .vnx-version (or .vnx/config.yml) pin, but
# never cross the git boundary of the directory we started in: a nested project
# without its own pin must not leak onto a parent project's pin/root above the
# repository root.
find_project_root() {
  local start="$PWD"
  local git_root=""
  git_root="$(git -C "$start" rev-parse --show-toplevel 2>/dev/null)" || git_root=""

  local dir="$start"
  while [ "$dir" != "/" ]; do
    if [ -f "${dir}/.vnx-version" ] || [ -f "${dir}/.vnx/config.yml" ]; then
      echo "$dir"
      return 0
    fi
    # Stop at the git boundary — do not traverse above the repository root.
    if [ -n "$git_root" ] && [ "$dir" = "$git_root" ]; then
      break
    fi
    dir="$(dirname "$dir")"
  done

  if [ -n "$git_root" ]; then
    echo "$git_root"   # no pin inside the repo: the repo root is the project root
    return 0
  fi
  echo "$start"        # not in a git repo: fall back to the invocation dir
}

_vnx_project_root="$(find_project_root)"
if [ "$_vnx_project_root" = "$VNX_HOME" ]; then
  # Mis-detection (invoked from inside VNX_HOME itself): stay non-fatal and let
  # the inner resolver fall back to its own layout heuristics. Aborting here
  # would break legitimate admin invocations from the install dir.
  echo "[vnx-shim] WARNING: project root detection returned VNX_HOME; deferring to resolver" >&2
  echo "[vnx-shim] Run vnx from your project directory (where .vnx-version lives)" >&2
else
  export VNX_PROJECT_ROOT="$_vnx_project_root"
fi
unset _vnx_project_root

exec "${VNX_HOME}/bin/vnx" "$@"
