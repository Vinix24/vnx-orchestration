#!/usr/bin/env bash
# VNX Command: new-worktree
# One-command feature worktree creation flow.
#
# Creates a git worktree, then bootstraps it with all VNX primitives:
#   1. git worktree add (branch + directory)
#   2. vnx worktree-start (isolated .vnx-data + intelligence snapshot)
#   3. bootstrap-skills, bootstrap-terminals, bootstrap-hooks
#   4. regen-settings --merge
#   5. .venv symlink (if main repo has one)
#   6. Scaffold plan files (FEATURE_PLAN.md, PR_QUEUE.md)
#   7. vnx doctor (validation)
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_new_worktree() {
  local name=""
  local branch=""
  local base_ref="HEAD"
  local preset=""
  local skip_doctor=false

  while [ $# -gt 0 ]; do
    case "$1" in
      --branch|-b)
        branch="$2"; shift 2 ;;
      --branch=*)
        branch="${1#*=}"; shift ;;
      --base)
        base_ref="$2"; shift 2 ;;
      --base=*)
        base_ref="${1#*=}"; shift ;;
      --preset)
        preset="$2"; shift 2 ;;
      --preset=*)
        preset="${1#*=}"; shift ;;
      --skip-doctor)
        skip_doctor=true; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx new-worktree <name> [options]

Creates a fully bootstrapped feature worktree in one command.

Arguments:
  <name>              Worktree name (directory: \${PROJECT_ROOT}-wt-<name>)

Options:
  --branch, -b <ref>  Branch name (default: feature/<name>)
  --base <ref>        Base ref to branch from (default: HEAD)
  --preset <name>     Copy startup preset into worktree
  --skip-doctor       Skip final doctor validation
  -h, --help          Show this help

Flow:
  1. Create git worktree and branch
  2. Initialize isolated .vnx-data (worktree-start)
  3. Bootstrap skills, terminals, hooks
  4. Merge VNX settings
  5. Link .venv from main repo
  6. Scaffold plan files if missing
  7. Validate with vnx doctor
HELP
        return 0
        ;;
      -*)
        err "[new-worktree] Unknown option: $1"
        return 1
        ;;
      *)
        if [ -z "$name" ]; then
          name="$1"
        else
          err "[new-worktree] Unexpected argument: $1"
          return 1
        fi
        shift
        ;;
    esac
  done

  if [ -z "$name" ]; then
    err "[new-worktree] Name is required. Usage: vnx new-worktree <name>"
    return 1
  fi

  # Derive paths
  local wt_dir="${PROJECT_ROOT}-wt-${name}"
  if [ -z "$branch" ]; then
    branch="feature/${name}"
  fi

  # ── Step 1: Create git worktree ──────────────────────────────────────
  if [ -d "$wt_dir" ]; then
    log "[new-worktree] Worktree already exists: $wt_dir"
    local existing_branch
    existing_branch="$(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    log "[new-worktree] Branch: $existing_branch"
  else
    log "[new-worktree] Creating worktree: $wt_dir (branch: $branch, base: $base_ref)"

    # Check if branch already exists
    if git -C "$PROJECT_ROOT" rev-parse --verify "$branch" >/dev/null 2>&1; then
      # Branch exists — attach worktree to it without -b
      git -C "$PROJECT_ROOT" worktree add "$wt_dir" "$branch"
    else
      # Create new branch from base_ref
      git -C "$PROJECT_ROOT" worktree add -b "$branch" "$wt_dir" "$base_ref"
    fi

    if [ ! -d "$wt_dir" ]; then
      err "[new-worktree] Failed to create worktree at: $wt_dir"
      return 1
    fi
    log "[new-worktree] Git worktree created"
  fi

  # ── Step 2: worktree-start (isolated .vnx-data) ─────────────────────
  # Override PROJECT_ROOT to the worktree for all subsequent operations.
  local _saved_project_root="$PROJECT_ROOT"
  local _saved_vnx_data_dir="${VNX_DATA_DIR:-}"
  local _saved_vnx_state_dir="${VNX_STATE_DIR:-}"
  local _saved_vnx_dispatch_dir="${VNX_DISPATCH_DIR:-}"
  local _saved_vnx_logs_dir="${VNX_LOGS_DIR:-}"
  local _saved_vnx_pids_dir="${VNX_PIDS_DIR:-}"
  local _saved_vnx_locks_dir="${VNX_LOCKS_DIR:-}"
  local _saved_vnx_reports_dir="${VNX_REPORTS_DIR:-}"
  local _saved_vnx_db_dir="${VNX_DB_DIR:-}"
  local _saved_vnx_skills_dir="${VNX_SKILLS_DIR:-}"

  export PROJECT_ROOT="$wt_dir"
  # Reset data dirs so worktree-start derives them from the new PROJECT_ROOT
  export VNX_DATA_DIR="$wt_dir/.vnx-data"
  export VNX_STATE_DIR="$VNX_DATA_DIR/state"
  export VNX_DISPATCH_DIR="$VNX_DATA_DIR/dispatches"
  export VNX_LOGS_DIR="$VNX_DATA_DIR/logs"
  export VNX_PIDS_DIR="$VNX_DATA_DIR/pids"
  export VNX_LOCKS_DIR="$VNX_DATA_DIR/locks"
  export VNX_REPORTS_DIR="$VNX_DATA_DIR/unified_reports"
  export VNX_DB_DIR="$VNX_DATA_DIR/database"

  # Update skills dir for the worktree
  if [ -d "$wt_dir/.claude/skills" ]; then
    export VNX_SKILLS_DIR="$wt_dir/.claude/skills"
  fi

  # _detect_worktree_context needs to detect the worktree properly
  log "[new-worktree] Initializing isolated .vnx-data..."
  cmd_worktree_start
  local wt_start_rc=$?
  if [ $wt_start_rc -ne 0 ]; then
    err "[new-worktree] worktree-start failed (rc=$wt_start_rc)"
    _new_worktree_restore_env "$_saved_project_root" "$_saved_vnx_data_dir" \
      "$_saved_vnx_state_dir" "$_saved_vnx_dispatch_dir" "$_saved_vnx_logs_dir" \
      "$_saved_vnx_pids_dir" "$_saved_vnx_locks_dir" "$_saved_vnx_reports_dir" \
      "$_saved_vnx_db_dir" "$_saved_vnx_skills_dir"
    return 1
  fi

  # ── Step 3: Bootstrap primitives ─────────────────────────────────────
  log "[new-worktree] Running bootstrap-skills..."
  cmd_bootstrap_skills || log "[new-worktree] WARN: bootstrap-skills had issues (non-fatal)"

  log "[new-worktree] Running bootstrap-terminals..."
  cmd_bootstrap_terminals || log "[new-worktree] WARN: bootstrap-terminals had issues (non-fatal)"

  log "[new-worktree] Running bootstrap-hooks..."
  cmd_bootstrap_hooks || log "[new-worktree] WARN: bootstrap-hooks had issues (non-fatal)"

  # ── Step 4: Merge settings ──────────────────────────────────────────
  log "[new-worktree] Running regen-settings --merge..."
  if ! type cmd_regen_settings &>/dev/null; then
    _load_command "regen_settings" 2>/dev/null || true
  fi
  if type cmd_regen_settings &>/dev/null; then
    cmd_regen_settings --merge --no-backup || log "[new-worktree] WARN: regen-settings had issues (non-fatal)"
  else
    log "[new-worktree] WARN: regen-settings not available, skipping settings merge"
  fi

  # ── Step 5: Link .venv from main repo ────────────────────────────────
  local main_venv="$_saved_project_root/.venv"
  local wt_venv="$wt_dir/.venv"
  if [ -d "$main_venv" ] && [ ! -e "$wt_venv" ]; then
    ln -s "$main_venv" "$wt_venv"
    log "[new-worktree] Linked .venv: $wt_venv -> $main_venv"
  elif [ -e "$wt_venv" ]; then
    log "[new-worktree] .venv already exists in worktree"
  else
    log "[new-worktree] WARN: No .venv found in main repo at $main_venv"
  fi

  # ── Step 6: Scaffold plan files ──────────────────────────────────────
  local tmpl_dir="$VNX_HOME/templates"

  if [ ! -f "$wt_dir/FEATURE_PLAN.md" ]; then
    if [ -f "$tmpl_dir/FEATURE_PLAN_TEMPLATE.md" ]; then
      sed -e "s|\[Feature Name\]|${name}|g" \
          "$tmpl_dir/FEATURE_PLAN_TEMPLATE.md" > "$wt_dir/FEATURE_PLAN.md"
      log "[new-worktree] Scaffolded FEATURE_PLAN.md (from template)"
    else
      cat > "$wt_dir/FEATURE_PLAN.md" <<PLAN
# Feature: ${name}

**Status**: DRAFT
**Branch**: $branch
**Created**: $(date -u +%Y-%m-%dT%H:%M:%SZ)

## Description

_Add feature description here._

## PR Queue

_Define PRs here._
PLAN
      log "[new-worktree] Scaffolded FEATURE_PLAN.md (inline)"
    fi
  fi

  if [ ! -f "$wt_dir/PR_QUEUE.md" ]; then
    if [ -f "$tmpl_dir/PR_QUEUE_TEMPLATE.md" ]; then
      sed -e "s|\[Feature Name\]|${name}|g" \
          "$tmpl_dir/PR_QUEUE_TEMPLATE.md" > "$wt_dir/PR_QUEUE.md"
      log "[new-worktree] Scaffolded PR_QUEUE.md (from template)"
    else
      cat > "$wt_dir/PR_QUEUE.md" <<QUEUE
# PR Queue - Feature: ${name}

## Progress Overview
Total: 0 PRs | Complete: 0 | Active: 0 | Queued: 0

## Status

_No PRs defined yet._
QUEUE
      log "[new-worktree] Scaffolded PR_QUEUE.md (inline)"
    fi
  fi

  # ── Step 7: Copy startup preset ──────────────────────────────────────
  if [ -n "$preset" ]; then
    local main_preset="$_saved_vnx_data_dir/startup_presets/${preset}.env"
    local wt_presets_dir="$VNX_DATA_DIR/startup_presets"
    if [ -f "$main_preset" ]; then
      mkdir -p "$wt_presets_dir"
      cp "$main_preset" "$wt_presets_dir/"
      log "[new-worktree] Copied preset: $preset"
    else
      log "[new-worktree] WARN: Preset not found: $main_preset"
    fi
  fi

  # ── Step 8: Validate with doctor ─────────────────────────────────────
  if [ "$skip_doctor" = true ]; then
    log "[new-worktree] Skipping doctor validation (--skip-doctor)"
  else
    log "[new-worktree] Running doctor validation..."
    if ! type cmd_doctor &>/dev/null; then
      _load_command "doctor" 2>/dev/null || true
    fi
    if type cmd_doctor &>/dev/null; then
      cmd_doctor || log "[new-worktree] WARN: doctor reported issues (review above)"
    else
      log "[new-worktree] WARN: doctor command not available"
    fi
  fi

  # ── Restore environment ─────────────────────────────────────────────
  _new_worktree_restore_env "$_saved_project_root" "$_saved_vnx_data_dir" \
    "$_saved_vnx_state_dir" "$_saved_vnx_dispatch_dir" "$_saved_vnx_logs_dir" \
    "$_saved_vnx_pids_dir" "$_saved_vnx_locks_dir" "$_saved_vnx_reports_dir" \
    "$_saved_vnx_db_dir" "$_saved_vnx_skills_dir"

  log ""
  log "════════════════════════════════════════════════════════════════"
  log " Worktree ready: $wt_dir"
  log " Branch: $branch"
  log ""
  log " Next steps:"
  log "   cd $wt_dir"
  log "   vnx start"
  log "════════════════════════════════════════════════════════════════"
}

# Helper: restore all environment variables to pre-worktree state
_new_worktree_restore_env() {
  export PROJECT_ROOT="$1"
  export VNX_DATA_DIR="$2"
  export VNX_STATE_DIR="$3"
  export VNX_DISPATCH_DIR="$4"
  export VNX_LOGS_DIR="$5"
  export VNX_PIDS_DIR="$6"
  export VNX_LOCKS_DIR="$7"
  export VNX_REPORTS_DIR="$8"
  export VNX_DB_DIR="$9"
  export VNX_SKILLS_DIR="${10}"
}
