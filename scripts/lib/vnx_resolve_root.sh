#!/usr/bin/env bash
# vnx_resolve_root.sh — Project-aware path resolution for VNX bash scripts.
# Bash equivalent of scripts/lib/project_root.py (issue #225 PR 2/4 Python port).
#
# Functions:
#   vnx_resolve_project_root <caller_file>  → exports VNX_PROJECT_ROOT
#   vnx_resolve_data_dir                    → exports VNX_DATA_DIR
#   vnx_resolve_state_dir                   → exports VNX_STATE_DIR
#   vnx_resolve_dispatch_dir                → exports VNX_DISPATCH_DIR
#
# Usage in scripts/:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/vnx_resolve_root.sh"
#   vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
#   vnx_resolve_data_dir
#   vnx_resolve_state_dir
#   vnx_resolve_dispatch_dir
#
# Usage in scripts/lib/:
#   source "$(dirname "${BASH_SOURCE[0]}")/vnx_resolve_root.sh"

# Resolve caller's physical directory, following symlinks.
# Uses cd -P to avoid requiring readlink -f (not available on macOS by default).
_vnx_rr_caller_dir() {
    local file="$1"
    local dir
    dir="$(cd "$(dirname "$file")" 2>/dev/null && pwd -P)" || dir=""
    printf '%s' "$dir"
}

# vnx_resolve_project_root <caller_file>
# Exports VNX_PROJECT_ROOT. Returns 1 on failure (no git repo and no fallback).
#
# Resolution order:
#   1. git rev-parse from caller_file's physical directory (symlink-resolved)
#   2. git rev-parse from CWD
#   3. $VNX_CANONICAL_ROOT env var (emits DeprecationWarning, will be removed v0.10.0)
#   4. return 1
vnx_resolve_project_root() {
    local caller="${1:-}"
    local git_root=""

    # 1. Caller file → physical directory → git toplevel
    if [ -n "$caller" ]; then
        local caller_dir
        caller_dir="$(_vnx_rr_caller_dir "$caller")"
        if [ -n "$caller_dir" ]; then
            git_root="$(git -C "$caller_dir" rev-parse --show-toplevel 2>/dev/null)" || git_root=""
            if [ -n "$git_root" ]; then
                VNX_PROJECT_ROOT="$(cd "$git_root" && pwd -P)"
                export VNX_PROJECT_ROOT
                return 0
            fi
        fi
    fi

    # 2. CWD → git toplevel
    git_root="$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)" || git_root=""
    if [ -n "$git_root" ]; then
        VNX_PROJECT_ROOT="$(cd "$git_root" && pwd -P)"
        export VNX_PROJECT_ROOT
        return 0
    fi

    # 3. VNX_CANONICAL_ROOT fallback (deprecated)
    if [ -n "${VNX_CANONICAL_ROOT:-}" ]; then
        printf '[vnx] DeprecationWarning: VNX_CANONICAL_ROOT env-var used for project root resolution. Prefer git-based resolution. This fallback will be removed in vnx-orchestration v0.10.0.\n' >&2
        VNX_PROJECT_ROOT="$(cd "$VNX_CANONICAL_ROOT" 2>/dev/null && pwd -P)" || VNX_PROJECT_ROOT="$VNX_CANONICAL_ROOT"
        export VNX_PROJECT_ROOT
        return 0
    fi

    # 4. Failure
    printf '[vnx] ERROR: Cannot resolve project root. Not in a git repo and VNX_CANONICAL_ROOT is not set. See https://github.com/Vinix24/vnx-orchestration/issues/225\n' >&2
    return 1
}

# vnx_resolve_data_dir
# Exports VNX_DATA_DIR. Requires VNX_PROJECT_ROOT to be set first.
#
# Honors VNX_DATA_DIR only when VNX_DATA_DIR_EXPLICIT=1 to prevent cross-project
# state pollution from inherited shell environments. Otherwise uses
# $VNX_PROJECT_ROOT/.vnx-data regardless of VNX_DATA_DIR value.
vnx_resolve_data_dir() {
    local explicit_val="${VNX_DATA_DIR:-}"
    local explicit_flag="${VNX_DATA_DIR_EXPLICIT:-}"

    if [ "$explicit_flag" = "1" ] && [ -n "$explicit_val" ]; then
        export VNX_DATA_DIR="$explicit_val"
        return 0
    fi

    if [ -n "$explicit_val" ] && [ "$explicit_flag" != "1" ]; then
        printf '[vnx] DeprecationWarning: VNX_DATA_DIR is set but VNX_DATA_DIR_EXPLICIT=1 is not. Ignoring VNX_DATA_DIR; using git-resolved project root. See issue #225.\n' >&2
    fi

    export VNX_DATA_DIR="${VNX_PROJECT_ROOT}/.vnx-data"
}

# vnx_resolve_state_dir
# Exports VNX_STATE_DIR=$VNX_DATA_DIR/state. Requires vnx_resolve_data_dir first.
vnx_resolve_state_dir() {
    export VNX_STATE_DIR="${VNX_DATA_DIR}/state"
}

# vnx_resolve_dispatch_dir
# Exports VNX_DISPATCH_DIR=$VNX_DATA_DIR/dispatches. Requires vnx_resolve_data_dir first.
vnx_resolve_dispatch_dir() {
    export VNX_DISPATCH_DIR="${VNX_DATA_DIR}/dispatches"
}
