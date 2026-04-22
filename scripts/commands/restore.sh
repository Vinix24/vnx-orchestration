#!/usr/bin/env bash
# cmd_restore — Restore VNX project state from a snapshot tarball
# Loaded by bin/vnx via _load_command restore

cmd_restore() {
  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: vnx restore <tarball> [--target <path>] [--force]

Restore .vnx-data from a snapshot tarball:
  - Validates tarball is gzip and contains .vnx-data/ at root
  - Prompts before overwriting an existing .vnx-data/ (unless --force)
  - Extracts to tarball's parent directory, or explicit --target
  - Offers to restore runtime DB from companion .sql dump (interactive only)

Options:
  --target <path>  Target directory (default: directory containing tarball)
  --force          Overwrite existing .vnx-data without prompting
HELP
    return 0
  fi

  PYTHONPATH="${VNX_HOME}/scripts/lib:${PYTHONPATH:-}" \
    python3 "${VNX_HOME}/scripts/lib/vnx_snapshot.py" restore "$@"
}
