#!/bin/bash
# Shared path resolver for VNX scripts.
# Allows environment overrides while defaulting to repo-relative paths.

__VNX_PATHS_SHELLOPTS="$(set +o)"
set -euo pipefail

# Resolve this file's directory without clobbering the caller's SCRIPT_DIR.
_VNX_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Always compute VNX_HOME from this script's location first (ground truth).
if [ "$(basename "$_VNX_PATHS_DIR")" = "lib" ]; then
  _VNX_HOME_FROM_SCRIPT="$(cd "$_VNX_PATHS_DIR/../.." && pwd)"
else
  _VNX_HOME_FROM_SCRIPT="$(cd "$_VNX_PATHS_DIR/.." && pwd)"
fi

# Default VNX_HOME to the dist root (parent of bin/ or scripts/).
# Only trust VNX_BIN/VNX_EXECUTABLE if they resolve to the same project tree
# as this script — prevents cross-project contamination from inherited env vars.
if [ -n "${VNX_BIN:-}" ]; then
  _VNX_HOME_FROM_BIN="$(cd "$(dirname "$VNX_BIN")/.." 2>/dev/null && pwd)" || _VNX_HOME_FROM_BIN=""
  if [ "$_VNX_HOME_FROM_BIN" = "$_VNX_HOME_FROM_SCRIPT" ]; then
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_BIN"
  else
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_SCRIPT"
  fi
  unset _VNX_HOME_FROM_BIN
elif [ -n "${VNX_EXECUTABLE:-}" ]; then
  _VNX_HOME_FROM_EXEC="$(cd "$(dirname "$VNX_EXECUTABLE")/.." 2>/dev/null && pwd)" || _VNX_HOME_FROM_EXEC=""
  if [ "$_VNX_HOME_FROM_EXEC" = "$_VNX_HOME_FROM_SCRIPT" ]; then
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_EXEC"
  else
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_SCRIPT"
  fi
  unset _VNX_HOME_FROM_EXEC
else
  VNX_HOME_DEFAULT="$_VNX_HOME_FROM_SCRIPT"
fi
unset _VNX_HOME_FROM_SCRIPT

# Default project root to the parent of VNX_HOME.
# Backward compatibility: if VNX_HOME lives under a legacy hidden directory layout, project root is two levels up.
if [ "$(basename "$VNX_HOME_DEFAULT")" = "vnx-system" ] && [ "$(basename "$(dirname "$VNX_HOME_DEFAULT")")" = ".claude" ]; then
  PROJECT_ROOT_DEFAULT="$(cd "$VNX_HOME_DEFAULT/../.." && pwd)"
else
  PROJECT_ROOT_DEFAULT="$(cd "$VNX_HOME_DEFAULT/.." && pwd)"
fi

# Guard against cross-project env contamination:
# If inherited VNX_HOME points to a different project tree than VNX_HOME_DEFAULT
# (computed from this script's location), reset it and all derived paths.
if [ -n "${VNX_HOME:-}" ] && [ "$VNX_HOME" != "$VNX_HOME_DEFAULT" ]; then
  # Preserve explicit VNX_DATA_DIR override (worktree isolation)
  _vnx_saved_data_dir="${VNX_DATA_DIR:-}"
  unset VNX_HOME VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR
  if [ -n "$_vnx_saved_data_dir" ]; then
    VNX_DATA_DIR="$_vnx_saved_data_dir"
  else
    unset VNX_DATA_DIR
  fi
  unset _vnx_saved_data_dir
fi
export VNX_HOME="${VNX_HOME:-$VNX_HOME_DEFAULT}"

# Derive PROJECT_ROOT from VNX_HOME.
# If VNX_HOME is under legacy layout, project root is two levels up.
if [ "$(basename "$VNX_HOME_DEFAULT")" = "vnx-system" ] && [ "$(basename "$(dirname "$VNX_HOME_DEFAULT")")" = ".claude" ]; then
  if [ -n "${PROJECT_ROOT:-}" ] && [ "$PROJECT_ROOT" != "$PROJECT_ROOT_DEFAULT" ]; then
    unset PROJECT_ROOT
  fi
  export PROJECT_ROOT="${PROJECT_ROOT:-$PROJECT_ROOT_DEFAULT}"
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

# ── Worktree PROJECT_ROOT override ──────────────────────────────
# When CWD is a git worktree of the same project, override PROJECT_ROOT
# and re-derive all data paths so each worktree gets its own session.
_vnx_cwd="$(pwd)"
if [ "$_vnx_cwd" != "$PROJECT_ROOT" ]; then
  _vnx_main_wt="$(git -C "$_vnx_cwd" rev-parse --path-format=absolute --git-common-dir 2>/dev/null | sed 's|/\.git$||')" || true
  if [ -n "${_vnx_main_wt:-}" ] && [ "$_vnx_main_wt" = "$PROJECT_ROOT" ]; then
    export PROJECT_ROOT="$_vnx_cwd"
    # Re-derive data paths for this worktree (unless explicitly overridden)
    if [ -z "${_vnx_saved_data_dir:-}" ] && { [ -z "${VNX_DATA_DIR:-}" ] || [ "$VNX_DATA_DIR" = "$_vnx_main_wt/.vnx-data" ]; }; then
      export VNX_DATA_DIR="$PROJECT_ROOT/.vnx-data"
      export VNX_STATE_DIR="$VNX_DATA_DIR/state"
      export VNX_DISPATCH_DIR="$VNX_DATA_DIR/dispatches"
      export VNX_LOGS_DIR="$VNX_DATA_DIR/logs"
      export VNX_PIDS_DIR="$VNX_DATA_DIR/pids"
      export VNX_LOCKS_DIR="$VNX_DATA_DIR/locks"
      export VNX_REPORTS_DIR="$VNX_DATA_DIR/unified_reports"
      export VNX_DB_DIR="$VNX_DATA_DIR/database"
    fi
    export VNX_INTELLIGENCE_DIR="$PROJECT_ROOT/.vnx-intelligence"
    # Re-derive skills dir
    if [ -d "$PROJECT_ROOT/.claude/skills" ]; then
      export VNX_SKILLS_DIR="$PROJECT_ROOT/.claude/skills"
    fi
  fi
fi
unset _vnx_cwd _vnx_main_wt

# Skills live outside dist; prefer a configured value, then fallback to known locations.
if [ -z "${VNX_SKILLS_DIR:-}" ]; then
  if [ -d "$PROJECT_ROOT/.claude/skills" ]; then
    export VNX_SKILLS_DIR="$PROJECT_ROOT/.claude/skills"
  else
    export VNX_SKILLS_DIR="$VNX_HOME/skills"
  fi
fi

# ── Resolver functions ────────────────────────────────────────
# These are callable from any script that sources vnx_paths.sh.
# They resolve runtime dependencies dynamically instead of
# relying on hardcoded version-pinned paths.

# Resolve the directory containing the node binary.
# Search order: VNX_NODE_PATH env override > nvm current > nvm installed versions > system PATH.
# Prints the directory path to stdout. Returns 1 if node is not found.
_resolve_node_path() {
  # Explicit override
  if [ -n "${VNX_NODE_PATH:-}" ] && [ -x "${VNX_NODE_PATH}/node" ]; then
    echo "$VNX_NODE_PATH"
    return 0
  fi

  # nvm: current default
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  if [ -d "$nvm_dir/versions/node" ]; then
    # Use nvm alias default if available
    local nvm_default=""
    if [ -f "$nvm_dir/alias/default" ]; then
      nvm_default="$(cat "$nvm_dir/alias/default" 2>/dev/null | tr -d '[:space:]')"
    fi
    # Try to match exact alias, then find latest installed version
    local candidate=""
    if [ -n "$nvm_default" ]; then
      # Match nvm default alias (could be "20", "v20.18.2", "lts/iron", etc.)
      local version_prefix="${nvm_default#v}"
      version_prefix="${version_prefix#lts/}"
      candidate="$(find "$nvm_dir/versions/node" -maxdepth 1 -name "v${version_prefix}*" -type d 2>/dev/null | sort -V | tail -1)"
    fi
    # Fallback: pick the latest installed version
    if [ -z "$candidate" ]; then
      candidate="$(find "$nvm_dir/versions/node" -maxdepth 1 -name 'v*' -type d 2>/dev/null | sort -V | tail -1)"
    fi
    if [ -n "$candidate" ] && [ -x "$candidate/bin/node" ]; then
      echo "$candidate/bin"
      return 0
    fi
  fi

  # System PATH fallback
  local sys_node
  sys_node="$(command -v node 2>/dev/null)" || true
  if [ -n "$sys_node" ]; then
    dirname "$sys_node"
    return 0
  fi

  return 1
}

# Resolve the path to the Python venv activation script.
# Search order: VNX_VENV_PATH env override > PROJECT_ROOT/.venv > main worktree .venv.
# Prints the activate script path to stdout. Returns 1 if not found.
_resolve_venv_path() {
  # Explicit override
  if [ -n "${VNX_VENV_PATH:-}" ] && [ -f "${VNX_VENV_PATH}/bin/activate" ]; then
    echo "${VNX_VENV_PATH}/bin/activate"
    return 0
  fi

  # Current project root (handles worktrees with symlinked .venv)
  if [ -f "${PROJECT_ROOT}/.venv/bin/activate" ]; then
    echo "${PROJECT_ROOT}/.venv/bin/activate"
    return 0
  fi

  # Main worktree fallback (when .venv wasn't symlinked into worktree)
  local main_root=""
  main_root="$(git -C "${PROJECT_ROOT}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null | sed 's|/\.git$||')" || true
  if [ -n "$main_root" ] && [ "$main_root" != "${PROJECT_ROOT}" ] && [ -f "$main_root/.venv/bin/activate" ]; then
    echo "$main_root/.venv/bin/activate"
    return 0
  fi

  return 1
}

# Resolve the project root for a given directory.
# Primarily useful for doctor validation and external callers.
# Uses git to find the repository root, falling back to the already-computed PROJECT_ROOT.
_resolve_project_root() {
  local target_dir="${1:-$(pwd)}"
  local git_root=""
  git_root="$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)" || true
  if [ -n "$git_root" ]; then
    echo "$git_root"
    return 0
  fi
  # Fallback: use the already-resolved PROJECT_ROOT
  echo "${PROJECT_ROOT:-}"
  return 0
}

# Activate the project venv if available. No-op if not found.
_activate_venv() {
  local venv_activate=""
  venv_activate="$(_resolve_venv_path 2>/dev/null)" || true
  if [ -n "$venv_activate" ]; then
    # shellcheck source=/dev/null
    source "$venv_activate"
    return 0
  fi
  return 1
}

# Export resolved node path for use in tmux send-keys and PATH construction.
# Sets VNX_RESOLVED_NODE_PATH. Returns 1 if node is not found.
_export_node_path() {
  local node_dir=""
  node_dir="$(_resolve_node_path 2>/dev/null)" || true
  if [ -n "$node_dir" ]; then
    export VNX_RESOLVED_NODE_PATH="$node_dir"
    return 0
  fi
  return 1
}

unset _VNX_PATHS_DIR
eval "$__VNX_PATHS_SHELLOPTS"
unset __VNX_PATHS_SHELLOPTS
