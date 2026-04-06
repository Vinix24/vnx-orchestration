#!/bin/bash
# Shared path resolver for VNX scripts.
# Allows environment overrides while defaulting to repo-relative paths.

__VNX_PATHS_SHELLOPTS="$(set +o)"
set -euo pipefail

# Resolve this file's directory without clobbering the caller's SCRIPT_DIR.
_VNX_PATHS_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

_vnx_canon_dir() {
  if [ -d "$1" ]; then
    (cd -P "$1" && pwd -P)
  else
    printf '%s\n' "$1"
  fi
}

_vnx_is_embedded_layout() {
  [ "$(basename "$1")" = "vnx-system" ] && [ "$(basename "$(dirname "$1")")" = ".claude" ]
}

_vnx_git_toplevel() {
  local git_root=""
  git_root="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || true
  if [ -n "$git_root" ]; then
    _vnx_canon_dir "$git_root"
  fi
}

_vnx_git_common_root() {
  local common_dir=""
  common_dir="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || true
  if [ -z "$common_dir" ]; then
    return 0
  fi
  if [ "$(basename "$common_dir")" = ".git" ]; then
    _vnx_canon_dir "$(dirname "$common_dir")"
  else
    _vnx_canon_dir "$common_dir"
  fi
}

# Always compute VNX_HOME from this script's location first (ground truth).
if [ "$(basename "$_VNX_PATHS_DIR")" = "lib" ]; then
  _VNX_HOME_FROM_SCRIPT="$(cd -P "$_VNX_PATHS_DIR/../.." && pwd -P)"
else
  _VNX_HOME_FROM_SCRIPT="$(cd -P "$_VNX_PATHS_DIR/.." && pwd -P)"
fi

# Default VNX_HOME to the dist root (parent of bin/ or scripts/).
# Only trust VNX_BIN/VNX_EXECUTABLE if they resolve to the same project tree
# as this script — prevents cross-project contamination from inherited env vars.
if [ -n "${VNX_BIN:-}" ]; then
  _VNX_HOME_FROM_BIN="$(cd -P "$(dirname "$VNX_BIN")/.." 2>/dev/null && pwd -P)" || _VNX_HOME_FROM_BIN=""
  if [ "$_VNX_HOME_FROM_BIN" = "$_VNX_HOME_FROM_SCRIPT" ]; then
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_BIN"
  else
    VNX_HOME_DEFAULT="$_VNX_HOME_FROM_SCRIPT"
  fi
  unset _VNX_HOME_FROM_BIN
elif [ -n "${VNX_EXECUTABLE:-}" ]; then
  _VNX_HOME_FROM_EXEC="$(cd -P "$(dirname "$VNX_EXECUTABLE")/.." 2>/dev/null && pwd -P)" || _VNX_HOME_FROM_EXEC=""
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
VNX_HOME_DEFAULT="$(_vnx_canon_dir "$VNX_HOME_DEFAULT")"

# Default runtime/bootstrap root.
# Embedded layout keeps runtime in the parent project; standalone repo/worktree
# layout keeps runtime local to the checkout itself.
_VNX_HOME_GIT_ROOT="$(_vnx_git_toplevel "$VNX_HOME_DEFAULT")"
if _vnx_is_embedded_layout "$VNX_HOME_DEFAULT"; then
  PROJECT_ROOT_DEFAULT="$(cd -P "$VNX_HOME_DEFAULT/../.." && pwd -P)"
elif [ -n "$_VNX_HOME_GIT_ROOT" ] && [ "$_VNX_HOME_GIT_ROOT" = "$VNX_HOME_DEFAULT" ]; then
  PROJECT_ROOT_DEFAULT="$VNX_HOME_DEFAULT"
else
  PROJECT_ROOT_DEFAULT="$(cd -P "$VNX_HOME_DEFAULT/.." && pwd -P)"
fi

# Canonical repo root owns git-tracked intelligence/provenance.
VNX_CANONICAL_ROOT_DEFAULT="$VNX_HOME_DEFAULT"
if [ -n "$_VNX_HOME_GIT_ROOT" ] && [ "$_VNX_HOME_GIT_ROOT" = "$VNX_HOME_DEFAULT" ]; then
  _VNX_HOME_COMMON_ROOT="$(_vnx_git_common_root "$VNX_HOME_DEFAULT")"
  if [ -n "$_VNX_HOME_COMMON_ROOT" ]; then
    VNX_CANONICAL_ROOT_DEFAULT="$_VNX_HOME_COMMON_ROOT"
  fi
fi
unset _VNX_HOME_GIT_ROOT _VNX_HOME_COMMON_ROOT

# Guard against cross-project env contamination:
# If inherited VNX_HOME points to a different project tree than VNX_HOME_DEFAULT
# (computed from this script's location), reset it and all derived paths.
if [ -n "${VNX_HOME:-}" ] && [ "$VNX_HOME" != "$VNX_HOME_DEFAULT" ]; then
  # Preserve explicit VNX_DATA_DIR override (worktree isolation)
  _vnx_saved_data_dir="${VNX_DATA_DIR:-}"
  unset VNX_HOME PROJECT_ROOT VNX_CANONICAL_ROOT VNX_INTELLIGENCE_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR
  if [ -n "$_vnx_saved_data_dir" ]; then
    VNX_DATA_DIR="$_vnx_saved_data_dir"
  else
    unset VNX_DATA_DIR
  fi
  unset _vnx_saved_data_dir
fi
export VNX_HOME="${VNX_HOME:-$VNX_HOME_DEFAULT}"

# Reject inherited PROJECT_ROOT/VNX_CANONICAL_ROOT values unless they match the
# resolver's current layout model exactly.
if [ -n "${PROJECT_ROOT:-}" ]; then
  _vnx_current_project_root="$(cd "$PROJECT_ROOT" 2>/dev/null && pwd)" || _vnx_current_project_root="$PROJECT_ROOT"
  if [ "$_vnx_current_project_root" != "$PROJECT_ROOT_DEFAULT" ]; then
    unset PROJECT_ROOT
  fi
fi
if [ -n "${VNX_CANONICAL_ROOT:-}" ]; then
  _vnx_current_canonical_root="$(cd "$VNX_CANONICAL_ROOT" 2>/dev/null && pwd)" || _vnx_current_canonical_root="$VNX_CANONICAL_ROOT"
  if [ "$_vnx_current_canonical_root" != "$VNX_CANONICAL_ROOT_DEFAULT" ]; then
    unset VNX_CANONICAL_ROOT
  fi
fi
unset _vnx_current_project_root _vnx_current_canonical_root

export PROJECT_ROOT="${PROJECT_ROOT:-$PROJECT_ROOT_DEFAULT}"
export VNX_CANONICAL_ROOT="${VNX_CANONICAL_ROOT:-$VNX_CANONICAL_ROOT_DEFAULT}"

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
export VNX_INTELLIGENCE_DIR="${VNX_INTELLIGENCE_DIR:-$VNX_CANONICAL_ROOT/.vnx-intelligence}"

# ── Worktree PROJECT_ROOT override ──────────────────────────────
# When CWD is a git worktree of the same project, override PROJECT_ROOT
# and re-derive all data paths so each worktree gets its own session.
_vnx_cwd="$(pwd)"
if [ "$_vnx_cwd" != "$PROJECT_ROOT" ]; then
  _vnx_cwd_git_root="$(_vnx_git_toplevel "$_vnx_cwd")"
  _vnx_cwd_common_root="$(_vnx_git_common_root "$_vnx_cwd")"
  _vnx_project_common_root="$(_vnx_git_common_root "$PROJECT_ROOT")"
  if [ -n "${_vnx_cwd_git_root:-}" ] && [ -n "${_vnx_cwd_common_root:-}" ] && [ "$_vnx_cwd_common_root" = "$_vnx_project_common_root" ] && [ "$_vnx_cwd_git_root" != "$PROJECT_ROOT" ]; then
    export PROJECT_ROOT="$_vnx_cwd_git_root"
    # Re-derive data paths for this worktree (unless explicitly overridden)
    if [ -z "${_vnx_saved_data_dir:-}" ] && { [ -z "${VNX_DATA_DIR:-}" ] || [ "$VNX_DATA_DIR" = "$_vnx_project_common_root/.vnx-data" ]; }; then
      export VNX_DATA_DIR="$PROJECT_ROOT/.vnx-data"
      export VNX_STATE_DIR="$VNX_DATA_DIR/state"
      export VNX_DISPATCH_DIR="$VNX_DATA_DIR/dispatches"
      export VNX_LOGS_DIR="$VNX_DATA_DIR/logs"
      export VNX_PIDS_DIR="$VNX_DATA_DIR/pids"
      export VNX_LOCKS_DIR="$VNX_DATA_DIR/locks"
      export VNX_REPORTS_DIR="$VNX_DATA_DIR/unified_reports"
      export VNX_DB_DIR="$VNX_DATA_DIR/database"
    fi
    # Re-derive skills dir
    if [ -d "$PROJECT_ROOT/.claude/skills" ]; then
      export VNX_SKILLS_DIR="$PROJECT_ROOT/.claude/skills"
    fi
  fi
fi
unset _vnx_cwd _vnx_cwd_git_root _vnx_cwd_common_root _vnx_project_common_root

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

unset _VNX_PATHS_DIR
unset -f _vnx_canon_dir _vnx_is_embedded_layout _vnx_git_toplevel _vnx_git_common_root
eval "$__VNX_PATHS_SHELLOPTS"
unset __VNX_PATHS_SHELLOPTS
