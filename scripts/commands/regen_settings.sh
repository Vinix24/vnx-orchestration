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

  # Wave 4 PR-4: never write settings.json into an immutable central install.
  # The merge/full target is "$PROJECT_ROOT/.claude/settings.json". If PROJECT_ROOT
  # mis-resolved onto VNX_HOME (the install-central bug), writing there would
  # contaminate the shared, versioned code tree. The guard is marker-gated
  # (only fires when .vnx-install-mode=central), so embedded and standalone-dev
  # layouts — where PROJECT_ROOT == VNX_HOME is legitimate — are unaffected.
  # --validate is read-only and intentionally exempt.
  if [ "$mode" = "--merge" ] || [ "$mode" = "--full" ]; then
    if type _guard_not_vnx_home >/dev/null 2>&1; then
      _guard_not_vnx_home "$PROJECT_ROOT" || return 1
    fi
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

  # Co-run worker_permissions merge so both managed files stay in sync.
  # Only fires for --merge and --full (not --validate which is read-only and already returned).
  if [ "$mode" = "--merge" ] || [ "$mode" = "--full" ]; then
    local wp_merge_script="$VNX_HOME/scripts/vnx_worker_permissions_merge.py"
    if [ -f "$wp_merge_script" ]; then
      local wp_args=("$mode" "--project-root" "$PROJECT_ROOT" "--vnx-home" "$VNX_HOME")
      [ "$dry_run" -eq 1 ]    && wp_args+=("--dry-run")
      [ "$no_backup" -eq 1 ]  && wp_args+=("--no-backup")
      # --json for settings.json output does not apply to worker_permissions output
      python3 "$wp_merge_script" "${wp_args[@]}" \
        || log "[regen-settings] WARN: worker_permissions merge had issues (non-fatal)"
    fi
  fi
}
