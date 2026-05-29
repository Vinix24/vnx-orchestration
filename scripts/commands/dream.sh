#!/usr/bin/env bash
# VNX command: dream — auto-dream memory consolidation (ADR-019)
# Sourced by bin/vnx. All VNX_HOME, VNX_STATE_DIR, log, err globals available.
#
# Subcommands:
#   run --project-id <id>               Run a consolidation cycle
#   install-scheduler --project-id <id> Install + activate nightly schedule
#   uninstall-scheduler                 Remove nightly schedule
#   status                              Show scheduler status

cmd_dream() {
  local subcmd="${1:-help}"
  shift || true

  local _dream_py="${VNX_HOME}/scripts/dream"
  local _pypath="${VNX_HOME}/scripts/lib:${VNX_HOME}/scripts/dream:${PYTHONPATH:-}"

  case "$subcmd" in
    run)
      PYTHONPATH="$_pypath" python3 "${_dream_py}/consolidator.py" "$@"
      ;;
    install-scheduler)
      PYTHONPATH="$_pypath" python3 "${_dream_py}/scheduler.py" install "$@"
      ;;
    uninstall-scheduler)
      PYTHONPATH="$_pypath" python3 "${_dream_py}/scheduler.py" uninstall "$@"
      ;;
    status)
      PYTHONPATH="$_pypath" python3 "${_dream_py}/scheduler.py" status "$@"
      ;;
    help|-h|--help)
      cat <<'USAGE'
Usage: vnx dream <subcommand> [options]

Subcommands:
  run --project-id <id>                Run memory consolidation cycle (ADR-019)
  install-scheduler --project-id <id>  Install nightly scheduler
                                       macOS: LaunchAgent (loads at 03:00)
                                       Linux: crontab entry (runs at 03:00)
  uninstall-scheduler                  Remove nightly scheduler
  status                               Show scheduler install status

Options for install-scheduler:
  --project-id <id>   Project to consolidate (required, ADR-007)
  --vnx-bin <path>    Path to vnx binary (default: which vnx)
  --project-root <p>  Project root override (default: git-resolved)
  --no-load           Write config but do not activate (CI / dry-run)
USAGE
      ;;
    *)
      err "[dream] Unknown subcommand: $subcmd"
      err "[dream] Run 'vnx dream help' for usage."
      return 1
      ;;
  esac
}
