#!/bin/bash
# Shared path resolver for VNX scripts.
# Allows environment overrides while defaulting to repo-relative paths.

__VNX_PATHS_SHELLOPTS="$(set +o)"
set -euo pipefail

# Resolve this file's directory without clobbering the caller's SCRIPT_DIR.
_VNX_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default VNX_HOME to the dist root (parent of bin/ or scripts/).
if [ -n "${VNX_BIN:-}" ]; then
  VNX_HOME_DEFAULT="$(cd "$(dirname "$VNX_BIN")/.." && pwd)"
elif [ -n "${VNX_EXECUTABLE:-}" ]; then
  VNX_HOME_DEFAULT="$(cd "$(dirname "$VNX_EXECUTABLE")/.." && pwd)"
elif [ "$(basename "$_VNX_PATHS_DIR")" = "lib" ]; then
  VNX_HOME_DEFAULT="$(cd "$_VNX_PATHS_DIR/../.." && pwd)"
else
  VNX_HOME_DEFAULT="$(cd "$_VNX_PATHS_DIR/.." && pwd)"
fi

# Default project root to the parent of VNX_HOME.
# Backward compatibility: if VNX_HOME lives under a legacy hidden directory layout, project root is two levels up.
if [ "$(basename "$VNX_HOME_DEFAULT")" = "vnx-system" ] && [ "$(basename "$(dirname "$VNX_HOME_DEFAULT")")" = ".claude" ]; then
  PROJECT_ROOT_DEFAULT="$(cd "$VNX_HOME_DEFAULT/../.." && pwd)"
else
  PROJECT_ROOT_DEFAULT="$(cd "$VNX_HOME_DEFAULT/.." && pwd)"
fi

export VNX_HOME="${VNX_HOME:-$VNX_HOME_DEFAULT}"

# Guard against cross-project env contamination:
# If VNX_HOME is under the legacy hidden vnx dir layout, trust derived project root.
if [ "$(basename "$VNX_HOME_DEFAULT")" = "vnx-system" ] && [ "$(basename "$(dirname "$VNX_HOME_DEFAULT")")" = ".claude" ]; then
  if [ -n "${PROJECT_ROOT:-}" ] && [ "$PROJECT_ROOT" != "$PROJECT_ROOT_DEFAULT" ]; then
    # Preserve explicit VNX_DATA_DIR override (worktree isolation)
    _vnx_saved_data_dir="${VNX_DATA_DIR:-}"
    unset VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR
    if [ -n "$_vnx_saved_data_dir" ]; then
      VNX_DATA_DIR="$_vnx_saved_data_dir"
    else
      unset VNX_DATA_DIR
    fi
    unset _vnx_saved_data_dir
  fi
  export PROJECT_ROOT="$PROJECT_ROOT_DEFAULT"
else
  export PROJECT_ROOT="${PROJECT_ROOT:-$PROJECT_ROOT_DEFAULT}"
fi

# Data directory (runtime root).
export VNX_DATA_DIR="${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}"
export VNX_STATE_DIR="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}"
export VNX_DISPATCH_DIR="${VNX_DISPATCH_DIR:-$VNX_DATA_DIR/dispatches}"
export VNX_LOGS_DIR="${VNX_LOGS_DIR:-$VNX_DATA_DIR/logs}"
export VNX_PIDS_DIR="${VNX_PIDS_DIR:-$VNX_DATA_DIR/pids}"
export VNX_LOCKS_DIR="${VNX_LOCKS_DIR:-$VNX_DATA_DIR/locks}"
export VNX_REPORTS_DIR="${VNX_REPORTS_DIR:-$VNX_DATA_DIR/unified_reports}"
export VNX_DB_DIR="${VNX_DB_DIR:-$VNX_DATA_DIR/database}"
export LEGACY_REPORTS_DIR="${LEGACY_REPORTS_DIR:-$VNX_HOME/unified_reports}"

# Git-tracked intelligence directory (portable across worktrees).
export VNX_INTELLIGENCE_DIR="${VNX_INTELLIGENCE_DIR:-$PROJECT_ROOT/.vnx-intelligence}"

# Skills live outside dist; prefer a configured value, then fallback to known locations.
if [ -z "${VNX_SKILLS_DIR:-}" ]; then
  if [ -d "$PROJECT_ROOT/.claude/skills" ]; then
    export VNX_SKILLS_DIR="$PROJECT_ROOT/.claude/skills"
  else
    export VNX_SKILLS_DIR="$VNX_HOME/skills"
  fi
fi

unset _VNX_PATHS_DIR
eval "$__VNX_PATHS_SHELLOPTS"
unset __VNX_PATHS_SHELLOPTS
