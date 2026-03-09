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

cmd_init_terminals() {
    local base_ref="${1:-main}"
    for t in T1 T2 T3; do
        local track
        case "$t" in T1) track="A";; T2) track="B";; T3) track="C";; esac
        local wt_dir="${PROJECT_ROOT}-wt-${t}"
        local branch="track/${track}"

        if [ -d "$wt_dir" ]; then
            echo "[$t] Worktree exists: $wt_dir ($(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown))"
        else
            git -C "$PROJECT_ROOT" worktree add -b "$branch" "$wt_dir" "$base_ref" 2>/dev/null || \
            git -C "$PROJECT_ROOT" worktree add "$wt_dir" "$branch" 2>/dev/null || {
                echo "[$t] ERROR: Failed to create worktree (branch $branch may already exist)"
                continue
            }
            # Symlink .venv (shared across all worktrees)
            ln -sf "${PROJECT_ROOT}/.venv" "$wt_dir/.venv"
            # Symlink .claude/vnx-system (orchestration is shared, read-only for workers)
            mkdir -p "$wt_dir/.claude" 2>/dev/null || true
            ln -sf "${PROJECT_ROOT}/.claude/vnx-system" "$wt_dir/.claude/vnx-system" 2>/dev/null || true
            # Symlink .vnx-data (runtime data is shared)
            ln -sf "${PROJECT_ROOT}/.vnx-data" "$wt_dir/.vnx-data" 2>/dev/null || true
            echo "[$t] Created: $wt_dir (branch: $branch)"
        fi
    done
}

cmd_sync() {
    local sync_errors=0
    for t in T1 T2 T3; do
        local wt_dir="${PROJECT_ROOT}-wt-${t}"
        [ -d "$wt_dir" ] || continue
        local branch
        branch="$(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
        echo "[$t] Syncing $branch with main..."
        if git -C "$wt_dir" fetch origin main 2>/dev/null && \
           git -C "$wt_dir" rebase origin/main 2>/dev/null; then
            echo "[$t] OK — rebased on origin/main"
        else
            echo "[$t] CONFLICT — manual resolve needed in $wt_dir"
            git -C "$wt_dir" rebase --abort 2>/dev/null || true
            sync_errors=$((sync_errors + 1))
        fi
    done
    [ "$sync_errors" -eq 0 ] || echo "WARNING: $sync_errors worktree(s) had conflicts"
}

cmd_list() {
    echo "VNX-managed worktrees:"
    git -C "$PROJECT_ROOT" worktree list | grep -- "-wt-" || echo "  (none)"
}

case "${1:-help}" in
    create) shift; cmd_create "$@" ;;
    remove) shift; cmd_remove "$@" ;;
    init-terminals) shift; cmd_init_terminals "$@" ;;
    sync)   cmd_sync ;;
    list)   cmd_list ;;
    *)
        echo "Usage: vnx_worktree_setup.sh {create|remove|init-terminals|sync|list} [args...]"
        echo ""
        echo "  create <name> [base_ref]     Create worktree at <project>-wt-<name>"
        echo "  remove <name>                Remove worktree (fails if dirty)"
        echo "  init-terminals [base_ref]    Create T1/T2/T3 worktrees with track branches"
        echo "  sync                         Rebase all terminal worktrees on origin/main"
        echo "  list                         Show VNX-managed worktrees"
        exit 1
        ;;
esac
