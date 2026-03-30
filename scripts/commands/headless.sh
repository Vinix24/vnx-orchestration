#!/usr/bin/env bash
# VNX Command: headless
# Operator-facing inspection and status for headless CLI runs.
#
# PR-3: Provides structured views so operators can diagnose headless runs
# without manual file spelunking (G-R2).
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_headless() {
  local subcmd="${1:-help}"
  shift 2>/dev/null || true

  case "$subcmd" in
    list|inspect|summary)
      _headless_python "$subcmd" "$@"
      ;;
    -h|--help|help)
      cat <<HELP
Usage: vnx headless <command> [options]

Inspect and monitor headless CLI runs.

Commands:
  list      List headless runs (recent, active, failed, problems)
  inspect   Show detailed view of a single run
  summary   Show headless health dashboard

List options:
  --active      Show only active (running) runs
  --failed      Show only failed runs
  --problems    Show stale/hung runs requiring attention
  --state STATE Filter by state (init, running, succeeded, failed, ...)
  --limit N     Max runs to show (default 20)

Inspect:
  vnx headless inspect <run_id>    Full or prefix match

Global options:
  --json        Output as JSON
  -h, --help    Show this help

Examples:
  vnx headless list --active         # What's running now?
  vnx headless list --problems       # Anything stale or hung?
  vnx headless list --failed         # Recent failures
  vnx headless inspect abc123       # Details for run starting with abc123
  vnx headless summary              # Health dashboard
HELP
      return 0
      ;;
    *)
      err "[headless] Unknown subcommand: $subcmd"
      err "Run 'vnx headless --help' for usage."
      return 1
      ;;
  esac
}

_headless_python() {
  local subcmd="$1"
  shift

  local scripts_lib="$VNX_HOME/scripts/lib"
  local state_dir="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}"

  if ! command -v python3 &>/dev/null; then
    err "[headless] python3 required but not found"
    return 1
  fi

  if [ ! -f "$scripts_lib/headless_inspect.py" ]; then
    err "[headless] Inspection module not found: $scripts_lib/headless_inspect.py"
    return 1
  fi

  PYTHONPATH="$scripts_lib${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$scripts_lib/headless_inspect.py" \
      --state-dir "$state_dir" \
      "$subcmd" "$@"
}
