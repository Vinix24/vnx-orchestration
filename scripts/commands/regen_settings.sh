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
#            - hooks: VNX-owned handlers refreshed, project handlers kept
#                     (--replace-hooks: every handler replaced, as before OI-2057)
#   --full:  Generate complete settings.json from VNX template (first-time init)

# The tree this render comes from: the project's pin decides it (fleet PR-5).
#
# The hook commands and env values rendered into settings.json name a tree. Before
# PR-5 that tree was always $VNX_HOME — the tree the CLI happens to run from — so a
# project pinned to another version got hook commands naming the running tree instead
# of its pin. Resolution is one call to the resolver launchd starts already use
# (scripts/lib/pinned_engine.py), so the pin rules stay in one place.
#
# Sets REGEN_RENDER_HOME (the tree to render from) and REGEN_RENDER_PIN (the pin that
# decided it; empty when the declared tree was used). Returns 1 when the render must
# not happen at all: a dead, unreadable or escaping pin is a refusal, never a fallback
# to $VNX_HOME.
#
# A tree that predates the resolver (every install older than PR-3) cannot honour a pin
# at all; it renders from itself, exactly as before PR-5 — refusing there would make the
# command unusable on those installs. The tree that ships THIS file ships the resolver,
# and no pin rule is re-implemented here.
_regen_settings_render_tree() {
  local declared="$1"
  local checkout="$2"
  local resolver="$declared/scripts/lib/pinned_engine.py"
  local resolved=""
  local rc=0
  local render_home=""
  local pin_line=""

  REGEN_RENDER_HOME="$declared"
  REGEN_RENDER_PIN=""
  [ -f "$resolver" ] || return 0

  # stderr is folded in: a refusal is ONE line (exit 78) and must reach `err` as written.
  # `|| rc=$?` keeps `set -e` (bin/vnx runs under it) from aborting before the refusal
  # is read: without it a dead pin would leave the command with no message at all.
  resolved="$(python3 "$resolver" --checkout "$checkout" --source "$declared" 2>&1)" || rc=$?
  if [ "$rc" -eq 78 ]; then
    err "${resolved:-pinned_engine refused this render without a reason}"
    return 1
  fi
  if [ "$rc" -ne 0 ]; then
    err "[regen-settings] cannot resolve the render tree (resolver exit $rc): ${resolved:-no output}"
    return 1
  fi

  { IFS= read -r render_home; IFS= read -r pin_line || true; } <<<"$resolved"
  if [ ! -d "${render_home:-}" ]; then
    err "[regen-settings] the resolver named a render tree that is not a directory: ${render_home:-<empty>}"
    return 1
  fi
  case "$pin_line" in
    "")
      ;;
    pin=*)
      REGEN_RENDER_PIN="${pin_line#pin=}"
      ;;
    *)
      err "[regen-settings] unexpected resolver output: $pin_line"
      return 1
      ;;
  esac

  REGEN_RENDER_HOME="$render_home"
  if [ -n "$REGEN_RENDER_PIN" ]; then
    log "[regen-settings] project pinned to $REGEN_RENDER_PIN: rendering from $REGEN_RENDER_HOME"
  fi
  return 0
}

cmd_regen_settings() {
  local mode=""
  local dry_run=0
  local no_backup=0
  local json_output=0
  local replace_hooks=0

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
      --replace-hooks)
        replace_hooks=1
        ;;
      -h|--help)
        cat <<HELP
Usage: vnx regen-settings <--merge|--full|--validate> [options]

Modes:
  --merge      Merge VNX-owned keys into existing settings.json
  --full       Generate complete settings.json from VNX template
  --validate   Validate existing settings.json structure

Options:
  --dry-run       Show what would change without writing
  --no-backup     Skip backup of existing settings.json
  --json          Output result as JSON (for scripting)
  --replace-hooks With --merge: replace every hook with the template's set
                  (the pre-OI-2057 behaviour); project hooks are dropped
  -h, --help      Show this help

Render source:
  A project whose .vnx-version pins a version renders from that version's tree;
  with no pin, from the tree this command runs from. A pin that is not installed
  refuses the render (exit 1) instead of falling back to the running tree.

Merge semantics:
  env          VNX_* keys managed, project keys preserved
  permissions  allow/deny: union (deduplicated), deny takes precedence
               ask/additionalDirectories: preserved (project-owned)
  hooks        VNX-owned handlers refreshed in place, project handlers kept
               --replace-hooks instead replaces the whole hooks block, which
               restores the old behaviour and drops every project hook
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

  # --replace-hooks modifies --merge only. The merge engine refuses the
  # combination too, but the wrapper rejects it here so the operator gets the
  # reason instead of a python argparse error after a template render.
  if [ "$replace_hooks" -eq 1 ] && [ "$mode" != "--merge" ]; then
    err "[regen-settings] --replace-hooks only applies to --merge"
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

  # One resolution for the whole command: the settings merge, its --vnx-home and the
  # worker-permissions merge must name the SAME tree, or the two managed files drift.
  _regen_settings_render_tree "$VNX_HOME" "$PROJECT_ROOT" || return 1

  local merge_script="$REGEN_RENDER_HOME/scripts/vnx_settings_merge.py"
  if [ ! -f "$merge_script" ]; then
    err "[regen-settings] Missing merge script: $merge_script"
    return 1
  fi

  local cmd_args=("$mode" "--project-root" "$PROJECT_ROOT" "--vnx-home" "$REGEN_RENDER_HOME")

  if [ "$dry_run" -eq 1 ]; then
    cmd_args+=("--dry-run")
  fi
  if [ "$no_backup" -eq 1 ]; then
    cmd_args+=("--no-backup")
  fi
  if [ "$json_output" -eq 1 ]; then
    cmd_args+=("--json")
  fi
  if [ "$replace_hooks" -eq 1 ]; then
    cmd_args+=("--replace-hooks")
  fi

  python3 "$merge_script" "${cmd_args[@]}"

  # Co-run worker_permissions merge so both managed files stay in sync.
  # Only fires for --merge and --full (not --validate which is read-only and already returned).
  if [ "$mode" = "--merge" ] || [ "$mode" = "--full" ]; then
    local wp_merge_script="$REGEN_RENDER_HOME/scripts/vnx_worker_permissions_merge.py"
    if [ -f "$wp_merge_script" ]; then
      local wp_args=("$mode" "--project-root" "$PROJECT_ROOT" "--vnx-home" "$REGEN_RENDER_HOME")
      [ "$dry_run" -eq 1 ]    && wp_args+=("--dry-run")
      [ "$no_backup" -eq 1 ]  && wp_args+=("--no-backup")
      # --json for settings.json output does not apply to worker_permissions output
      python3 "$wp_merge_script" "${wp_args[@]}" \
        || log "[regen-settings] WARN: worker_permissions merge had issues (non-fatal)"
    fi
  fi
}
