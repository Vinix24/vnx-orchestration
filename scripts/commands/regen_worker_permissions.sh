#!/usr/bin/env bash
# VNX Command: regen-worker-permissions
# Overlay-merge for worker_permissions.yaml — mirrors regen-settings semantics.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, VNX_HOME, PROJECT_ROOT, etc.)
# are available when this runs.
#
# Merge semantics:
#   --merge: Overlay VNX-managed keys from the shipped template into the project
#            worker_permissions.yaml at PROJECT_ROOT/.vnx/worker_permissions.yaml.
#            - version, _vnx_meta: replaced (VNX-owned)
#            - profiles (VNX roles): unioned per-field (allowed_tools, denied_tools,
#              bash_allow_patterns, bash_deny_patterns); file_write_scope is UNIONED
#              so project-added paths (e.g. src/**) survive cutover
#            - profiles (project-only roles): preserved unchanged
#            - terminal_assignments: VNX baseline; project overrides win
#   --full:  Generate worker_permissions.yaml from the VNX template (first-time init)
#   --validate: Read-only structural validation

cmd_regen_worker_permissions() {
  local mode=""
  local dry_run=0
  local no_backup=0
  local json_output=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --merge)
        mode="--merge"
        ;;
      --full)
        mode="--full"
        ;;
      --validate)
        mode="--validate"
        ;;
      --dry-run)
        dry_run=1
        ;;
      --no-backup)
        no_backup=1
        ;;
      --json)
        json_output=1
        ;;
      -h|--help)
        cat <<HELP
Usage: vnx regen-worker-permissions <--merge|--full|--validate> [options]

Modes:
  --merge      Merge VNX template into existing .vnx/worker_permissions.yaml
  --full       Generate .vnx/worker_permissions.yaml from VNX template (first-time)
  --validate   Validate existing .vnx/worker_permissions.yaml structure

Options:
  --dry-run    Show what would change without writing
  --no-backup  Skip backup of existing file
  --json       Output result as JSON (for scripting)
  -h, --help   Show this help

Merge semantics:
  version / _vnx_meta          VNX-managed — replaced from template
  profiles (VNX roles)         Per-field union: VNX baseline + project extras
  profiles.<role>.file_write_scope  Union — project paths (e.g. src/**) survive
  profiles (project-only roles)  Preserved unchanged
  terminal_assignments          VNX baseline; project overrides win on collision

This command is idempotent and safe to run on every rc-cutover.
HELP
        return 0
        ;;
      *)
        err "[regen-worker-permissions] Unknown option: $1"
        return 1
        ;;
    esac
    shift
  done

  if [ -z "$mode" ]; then
    err "[regen-worker-permissions] Specify --merge, --full, or --validate"
    return 1
  fi

  # Central-install write guard: same as regen-settings.
  # --validate is read-only and exempt.
  if [ "$mode" = "--merge" ] || [ "$mode" = "--full" ]; then
    if type _guard_not_vnx_home >/dev/null 2>&1; then
      _guard_not_vnx_home "$PROJECT_ROOT" || return 1
    fi
  fi

  if ! command -v python3 &>/dev/null; then
    err "[regen-worker-permissions] python3 is required"
    return 1
  fi

  local merge_script="$VNX_HOME/scripts/vnx_worker_permissions_merge.py"
  if [ ! -f "$merge_script" ]; then
    err "[regen-worker-permissions] Missing merge script: $merge_script"
    return 1
  fi

  local cmd_args=("$mode" "--project-root" "$PROJECT_ROOT" "--vnx-home" "$VNX_HOME")

  [ "$dry_run" -eq 1 ]    && cmd_args+=("--dry-run")
  [ "$no_backup" -eq 1 ]  && cmd_args+=("--no-backup")
  [ "$json_output" -eq 1 ] && cmd_args+=("--json")

  python3 "$merge_script" "${cmd_args[@]}"
}
