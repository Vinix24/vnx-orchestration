#!/usr/bin/env bash
# VNX Command: worktree-release
# Governed release path for locked worktrees (OI-1052).
#
# Re-classifies every locked worktree, rescues committable/unpushed work
# to origin branches, then unlocks and removes the worktree.
# Default is dry-run: nothing is deleted without --apply.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_worktree_release() {
  local apply=0
  local json_output=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --apply)
        apply=1; shift ;;
      --dry-run)
        apply=0; shift ;;
      --json)
        json_output=1; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx worktree-release [options]

Re-classify every locked git worktree, rescue committable/unpushed work to
origin branches, then unlock and remove the worktree.

The default is dry-run: report what would happen without changing anything.
Pass --apply to actually unlock, rescue, and remove.

Options:
  --apply          Execute the release (default: dry-run only)
  --dry-run        Report only, make no changes (default)
  --json           Machine-readable JSON output
  -h, --help       Show this help

Classification:
  releasable       Clean working tree, all commits pushed — safe to remove
  committable      Uncommitted changes, no local-only commits — auto-commit
                   to a vnx-release/<branch> rescue branch on origin
  unpushed_commits Local commits not on origin — push the existing branch
  both             Both uncommitted changes AND unpushed commits — commit
                   then push
  unreachable      Worktree directory does not exist — skip
  error            Git operation failed — skip, leave locked

Safety:
  - Dry-run is the DEFAULT. Nothing is deleted without --apply.
  - A worktree whose work cannot be rescued remains locked.
  - Rescue pushes to origin (vnx-release/<branch> for salvage commits);
    the original local branch is deleted after successful push.

Examples:
  vnx worktree-release                    # dry-run: see what would happen
  vnx worktree-release --apply            # actually release all locked worktrees
  vnx worktree-release --json             # machine-readable dry-run
HELP
        return 0
        ;;
      -*)
        err "[worktree-release] Unknown option: $1"
        return 1
        ;;
      *)
        err "[worktree-release] Unexpected argument: $1"
        return 1
        ;;
    esac
  done

  # Resolve the Python script
  local release_py="$VNX_HOME/scripts/lib/worktree_release.py"
  if [ ! -f "$release_py" ]; then
    err "[worktree-release] Python module not found: $release_py"
    return 1
  fi

  # Resolve the Python interpreter
  local python_bin="${VNX_PYTHON:-python3}"
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    err "[worktree-release] Python interpreter not found: $python_bin"
    return 1
  fi

  # Ensure scripts/lib is on the path
  local lib_dir="$VNX_HOME/scripts/lib"

  local -a py_args=("$release_py" "--repo-root" "$PROJECT_ROOT")
  if [ "$apply" -eq 1 ]; then
    py_args+=("--apply")
  fi
  if [ "$json_output" -eq 1 ]; then
    py_args+=("--json")
  fi

  PYTHONPATH="$lib_dir:${PYTHONPATH:-}" "$python_bin" "${py_args[@]}"
  local rc=$?

  if [ "$apply" -eq 1 ] && [ $rc -eq 0 ]; then
    log "[worktree-release] Release complete. Report saved in: $VNX_REPORTS_DIR"
  fi

  return $rc
}
