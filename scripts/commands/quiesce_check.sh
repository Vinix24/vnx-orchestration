#!/usr/bin/env bash
# cmd_quiesce_check — Verify a VNX project is safe to snapshot/migrate
# Loaded by bin/vnx via _load_command quiesce-check (→ quiesce_check.sh)

cmd_quiesce_check() {
  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: vnx quiesce-check [<project-path>]

Verify project state is safe to migrate or snapshot:
  1. No dispatches in active/ younger than 1 hour
  2. No held terminal leases in runtime_coordination.db
  3. No in-flight review gate requests (request without result)
  4. No uncommitted git changes in project worktree

Exits 0 if quiescent, 1 if not. Prints which check(s) failed.
Default <project-path> is the current directory.

This command is read-only and never mutates state.
HELP
    return 0
  fi

  PYTHONPATH="${VNX_HOME}/scripts/lib:${PYTHONPATH:-}" \
    python3 "${VNX_HOME}/scripts/lib/vnx_snapshot.py" quiesce-check "${@:-.}"
}
