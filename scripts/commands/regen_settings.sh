#!/usr/bin/env bash
# VNX Command: regen-settings
# Extracted command for patch-based settings.json management.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, VNX_HOME, PROJECT_ROOT, etc.)
# are available when this runs.
#
# Merge semantics:
#   --merge: Overlay VNX-owned keys into existing settings.json
#            - env: VNX_* keys replaced, project keys preserved
#            - permissions.allow/deny: union (deduplicated), deny > allow
#            - permissions.ask/additionalDirectories: preserved
#            - hooks: replaced (VNX-owned)
#   --full:  Generate complete settings.json from VNX template (first-time init)

cmd_regen_settings() {
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
Usage: vnx regen-settings <--merge|--full|--validate> [options]

Modes:
  --merge      Merge VNX-owned keys into existing settings.json
  --full       Generate complete settings.json from VNX template
  --validate   Validate existing settings.json structure

Options:
  --dry-run    Show what would change without writing
  --no-backup  Skip backup of existing settings.json
  --json       Output result as JSON (for scripting)
  -h, --help   Show this help

Merge semantics:
  env          VNX_* keys managed, project keys preserved
  permissions  allow/deny: union (deduplicated), deny takes precedence
               ask/additionalDirectories: preserved (project-owned)
  hooks        Replaced entirely (VNX-owned)
HELP
        return 0
        ;;
      *)
        err "[regen-settings] Unknown option: $1"
        return 1
        ;;
    esac
    shift
  done

  if [ -z "$mode" ]; then
    err "[regen-settings] Specify --merge, --full, or --validate"
    return 1
  fi

  # Require python3
  if ! command -v python3 &>/dev/null; then
    err "[regen-settings] python3 is required"
    return 1
  fi

  local merge_script="$VNX_HOME/scripts/vnx_settings_merge.py"
  if [ ! -f "$merge_script" ]; then
    err "[regen-settings] Missing merge script: $merge_script"
    return 1
  fi

  local cmd_args=("$mode" "--project-root" "$PROJECT_ROOT" "--vnx-home" "$VNX_HOME")

  if [ "$dry_run" -eq 1 ]; then
    cmd_args+=("--dry-run")
  fi
  if [ "$no_backup" -eq 1 ]; then
    cmd_args+=("--no-backup")
  fi
  if [ "$json_output" -eq 1 ]; then
    cmd_args+=("--json")
  fi

  python3 "$merge_script" "${cmd_args[@]}"
}
