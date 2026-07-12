#!/usr/bin/env bash
# VNX Command: subsystems
# Thin wrapper around the pip-CLI `vnx subsystems` command (framework-status-
# audit-and-cockpit PR-3) — repo-local dual-CLI parity.

cmd_subsystems() {
  PYTHONPATH="$VNX_HOME:$VNX_HOME/scripts/lib:${PYTHONPATH:-}" \
    python3 -m vnx_cli.main subsystems "$@"
}
