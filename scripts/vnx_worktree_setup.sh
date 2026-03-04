#!/usr/bin/env bash
# Create or remove a git worktree for a feature plan.
# All agents work in the same worktree; dependencies prevent conflicts.
#
# Usage:
#   vnx_worktree_setup.sh create <name> [base_ref]
#   vnx_worktree_setup.sh remove <name>
#   vnx_worktree_setup.sh list
#
# Examples:
#   vnx_worktree_setup.sh create fp04       # worktree from HEAD
#   vnx_worktree_setup.sh create fp04 main  # worktree from main
#   vnx_worktree_setup.sh remove fp04
#   vnx_worktree_setup.sh list

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"

_worktree_path() {
    local name="$1"
    echo "${PROJECT_ROOT}-wt-${name}"
}

cmd_create() {
    local name="${1:?Usage: vnx_worktree_setup.sh create <name> [base_ref]}"
    local base_ref="${2:-HEAD}"
    local wt_dir
    wt_dir=$(_worktree_path "$name")
    local branch="vnx/${name}"

    if [ -d "$wt_dir" ]; then
        echo "Worktree already exists: $wt_dir"
        echo "Branch: $(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
        return 0
    fi

    echo "Creating worktree: $wt_dir (branch: $branch, base: $base_ref)"
    git -C "$PROJECT_ROOT" worktree add -b "$branch" "$wt_dir" "$base_ref"
    echo "Done. All agents can work in: $wt_dir"
}

cmd_remove() {
    local name="${1:?Usage: vnx_worktree_setup.sh remove <name>}"
    local wt_dir
    wt_dir=$(_worktree_path "$name")

    if [ ! -d "$wt_dir" ]; then
        echo "Worktree does not exist: $wt_dir"
        return 0
    fi

    # Safety: warn if there are uncommitted changes
    local dirty_count
    dirty_count=$(git -C "$wt_dir" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if [ "$dirty_count" -gt 0 ]; then
        echo "WARNING: $dirty_count uncommitted files in $wt_dir"
        echo "Aborting. Commit or discard changes first, or use: git worktree remove --force $wt_dir"
        return 1
    fi

    echo "Removing worktree: $wt_dir"
    git -C "$PROJECT_ROOT" worktree remove "$wt_dir"

    # Clean up branch if it was merged
    local branch="vnx/${name}"
    if git -C "$PROJECT_ROOT" branch --merged | grep -q "$branch"; then
        echo "Branch $branch is merged, deleting..."
        git -C "$PROJECT_ROOT" branch -d "$branch" 2>/dev/null || true
    else
        echo "Branch $branch is NOT merged, keeping it."
    fi
}

cmd_list() {
    echo "VNX-managed worktrees:"
    git -C "$PROJECT_ROOT" worktree list | grep -- "-wt-" || echo "  (none)"
}

case "${1:-help}" in
    create) shift; cmd_create "$@" ;;
    remove) shift; cmd_remove "$@" ;;
    list)   cmd_list ;;
    *)
        echo "Usage: vnx_worktree_setup.sh {create|remove|list} [args...]"
        echo ""
        echo "  create <name> [base_ref]  Create worktree at <project>-wt-<name>"
        echo "  remove <name>             Remove worktree (fails if dirty)"
        echo "  list                      Show VNX-managed worktrees"
        exit 1
        ;;
esac
