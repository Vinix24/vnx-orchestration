#!/usr/bin/env bash
# cmd_snapshot — VNX project-state snapshot
# Loaded by bin/vnx via _load_command snapshot

cmd_snapshot() {
  if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: vnx snapshot [<project-path>]

Create a snapshot of <project-path>/.vnx-data:
  - Tarball:  ~/vnx-snapshots/<slug>-<timestamp>.tar.gz
  - SQL dump: ~/vnx-snapshots/<slug>-runtime-<timestamp>.sql  (if DB exists)

Default <project-path> is the current directory.
HELP
    return 0
  fi

  PYTHONPATH="${VNX_HOME}/scripts/lib:${PYTHONPATH:-}" \
    python3 "${VNX_HOME}/scripts/lib/vnx_snapshot.py" snapshot "${@:-.}"
}
