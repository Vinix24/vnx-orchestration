#!/usr/bin/env bash
# VNX Command: finish-worktree
# Governance-aware worktree closure flow.
#
# Runs merge-preflight, then:
#   1. Stop worktree-scoped VNX processes
#   2. Merge intelligence back to main (non-destructive)
#   3. Remove git worktree
#   4. Delete branch (optional)
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_finish_worktree() {
  local name=""
  local delete_branch=false
  local force=false

  while [ $# -gt 0 ]; do
    case "$1" in
      --delete-branch)
        delete_branch=true; shift ;;
      --force|-f)
        force=true; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx finish-worktree <name> [options]

Closes a feature worktree with governance checks.

Arguments:
  <name>             Worktree name (matches directory: \${PROJECT_ROOT}-wt-<name>)

Options:
  --delete-branch    Delete the feature branch after worktree removal
  --force, -f        Proceed despite governance blockers (carry-over summary shown)
  -h, --help         Show this help

Flow:
  1. Run merge-preflight governance checks
  2. Stop worktree-scoped VNX processes
  3. Merge intelligence back to main (non-destructive)
  4. Remove git worktree
  5. Delete branch (if --delete-branch)
HELP
        return 0
        ;;
      -*)
        err "[finish-worktree] Unknown option: $1"
        return 1
        ;;
      *)
        if [ -z "$name" ]; then
          name="$1"
        else
          err "[finish-worktree] Unexpected argument: $1"
          return 1
        fi
        shift
        ;;
    esac
  done

  if [ -z "$name" ]; then
    err "[finish-worktree] Name is required. Usage: vnx finish-worktree <name>"
    return 1
  fi

  local wt_dir="${PROJECT_ROOT}-wt-${name}"
  if [ ! -d "$wt_dir" ]; then
    err "[finish-worktree] Worktree not found: $wt_dir"
    return 1
  fi

  local wt_data="$wt_dir/.vnx-data"
  local main_data="${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}"

  # ── Step 1: Governance preflight ───────────────────────────────────────
  log "[finish-worktree] Running merge preflight..."

  # Ensure merge_preflight is loaded
  if ! type cmd_merge_preflight &>/dev/null; then
    _load_command "merge_preflight" 2>/dev/null || true
  fi

  if type cmd_merge_preflight &>/dev/null; then
    if ! cmd_merge_preflight "$name"; then
      if [ "$force" = true ]; then
        log ""
        log "[finish-worktree] WARN: Governance issues detected — proceeding with --force"
        log "[finish-worktree] Carry-over items will be visible in the finish summary."
      else
        err "[finish-worktree] Governance check failed. Use --force to override."
        return 1
      fi
    fi
  else
    log "[finish-worktree] WARN: merge-preflight not available, skipping governance checks"
  fi

  # ── Step 2: Stop worktree-scoped processes ─────────────────────────────
  log "[finish-worktree] Stopping worktree-scoped processes..."
  _finish_wt_stop_processes "$wt_dir" "$wt_data"

  # ── Step 3: Intelligence merge-back (non-destructive) ──────────────────
  log "[finish-worktree] Merging intelligence back to main..."
  _finish_wt_merge_intelligence "$wt_dir" "$wt_data" "$main_data"

  # ── Step 4: Remove git worktree ────────────────────────────────────────
  local git_branch
  git_branch="$(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

  log "[finish-worktree] Removing git worktree: $wt_dir"
  if ! git -C "$PROJECT_ROOT" worktree remove "$wt_dir" --force 2>/dev/null; then
    # If removal fails (dirty), clean up .vnx-data first and retry
    if [ -d "$wt_data" ]; then
      rm -rf "$wt_data"
    fi
    if ! git -C "$PROJECT_ROOT" worktree remove "$wt_dir" --force 2>/dev/null; then
      err "[finish-worktree] Failed to remove worktree. Manual cleanup may be needed."
      err "[finish-worktree] Try: git -C $PROJECT_ROOT worktree remove $wt_dir --force"
      return 1
    fi
  fi

  # Clean up inbox relay on main
  rm -f "$main_data/inbox/wt-$(basename "$wt_dir").ndjson" 2>/dev/null || true

  # Prune stale worktree references
  git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true

  log "[finish-worktree] Worktree removed"

  # ── Step 5: Delete branch (optional) ──────────────────────────────────
  if [ "$delete_branch" = true ] && [ "$git_branch" != "unknown" ] && [ "$git_branch" != "HEAD" ]; then
    log "[finish-worktree] Deleting branch: $git_branch"
    if git -C "$PROJECT_ROOT" branch -d "$git_branch" 2>/dev/null; then
      log "[finish-worktree] Branch deleted: $git_branch"
    else
      log "[finish-worktree] WARN: Could not delete branch (may not be fully merged)."
      log "[finish-worktree] Use 'git branch -D $git_branch' to force-delete."
    fi
  fi

  # ── Summary ───────────────────────────────────────────────────────────
  log ""
  log "════════════════════════════════════════════════════════════════"
  log " Worktree finished: $name"
  log " Branch: $git_branch"
  if [ "$force" = true ]; then
    log ""
    log " NOTE: Finished with --force. Review carry-over items above."
  fi
  log "════════════════════════════════════════════════════════════════"
}

# ── Helper: Stop only worktree-scoped processes ──────────────────────────
_finish_wt_stop_processes() {
  local wt_dir="$1"
  local wt_data="$2"
  local stopped=0

  # Stop processes tracked by PID files in the worktree's .vnx-data
  if [ -d "$wt_data/pids" ]; then
    local pid_file
    for pid_file in "$wt_data/pids"/*.pid; do
      [ -f "$pid_file" ] || continue
      local pid proc_name
      pid="$(cat "$pid_file" 2>/dev/null)"
      proc_name="$(basename "${pid_file%.pid}")"

      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "[finish-worktree] Stopping $proc_name (PID: $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
        stopped=$((stopped + 1))
      fi
      rm -f "$pid_file" "${pid_file}.fingerprint" 2>/dev/null || true
    done
  fi

  # Kill any remaining processes scoped to this worktree's data directory
  if [ -d "$wt_data" ]; then
    local pid
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      [ "$pid" != "$$" ] || continue
      local _cmd
      _cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
      if [[ "$_cmd" == *"$wt_data"* ]]; then
        kill -TERM "$pid" 2>/dev/null || true
        stopped=$((stopped + 1))
      fi
    done < <(pgrep -f "$(basename "$wt_data")" 2>/dev/null || true)
  fi

  # Clean up stale locks
  if [ -d "$wt_data/locks" ]; then
    rm -rf "$wt_data/locks"/*.lock 2>/dev/null || true
  fi

  if [ "$stopped" -gt 0 ]; then
    sleep 1  # Brief pause for graceful shutdown
    log "[finish-worktree] Stopped $stopped process(es)"
  else
    log "[finish-worktree] No active worktree processes found"
  fi
}

# ── Helper: Non-destructive intelligence merge-back ──────────────────────
_finish_wt_merge_intelligence() {
  local wt_dir="$1"
  local wt_data="$2"
  local main_data="$3"

  if [ ! -d "$wt_data" ]; then
    log "[finish-worktree] No .vnx-data found, skipping intelligence merge"
    return 0
  fi

  # Save/restore env for worktree context operations
  local _saved_project_root="$PROJECT_ROOT"
  local _saved_vnx_data_dir="${VNX_DATA_DIR:-}"
  export PROJECT_ROOT="$wt_dir"
  export VNX_DATA_DIR="$wt_data"

  # Strategy 1: Export to git-tracked .vnx-intelligence/ (preferred)
  local merged=false
  if [ -d "$wt_dir/.vnx-intelligence" ] || [ -f "$wt_data/database/quality_intelligence.db" ]; then
    log "[finish-worktree] Exporting intelligence to .vnx-intelligence/..."
    if cmd_intelligence_export 2>/dev/null; then
      merged=true
    fi
  fi

  # Strategy 2: Fallback to legacy merge script (non-destructive)
  if [ "$merged" = false ]; then
    local merge_script="$VNX_HOME/scripts/vnx_worktree_merge_data.sh"
    if [ -f "$merge_script" ]; then
      log "[finish-worktree] Merging intelligence via merge script..."
      bash "$merge_script" "$wt_data" && merged=true
    fi
  fi

  # Restore env
  export PROJECT_ROOT="$_saved_project_root"
  export VNX_DATA_DIR="$_saved_vnx_data_dir"

  if [ "$merged" = true ]; then
    log "[finish-worktree] Intelligence merged back to main"
  else
    log "[finish-worktree] WARN: Intelligence merge failed. Data preserved until worktree removal."
  fi
}
