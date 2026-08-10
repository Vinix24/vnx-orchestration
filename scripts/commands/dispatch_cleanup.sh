#!/usr/bin/env bash
# VNX Command: dispatch-cleanup
# Governed cleanup for stale dispatch bundles (OI-1072).
#
# Scans dispatches/pending/ for directory bundles that were staged but never
# cleaned up after the door processed them. Classifies by age and receipt
# presence, then moves or skips accordingly.
# Default is dry-run: nothing is moved without --apply.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_dispatch_cleanup() {
  local apply=0
  local json_output=0
  local stale_days=7

  while [ $# -gt 0 ]; do
    case "$1" in
      --apply)
        apply=1; shift ;;
      --dry-run)
        apply=0; shift ;;
      --json)
        json_output=1; shift ;;
      --stale-days)
        stale_days="$2"; shift 2 ;;
      --stale-days=*)
        stale_days="${1#*=}"; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx dispatch-cleanup [options]

Scan dispatches/pending/ for stale directory bundles (dispatch-spec.json +
instruction.md) that were staged but never cleaned up after dispatch. Each
bundle is classified by age and whether a matching receipt exists in the
ledger.

The default is dry-run: report what would happen without changing anything.
Pass --apply to actually move bundles.

Options:
  --apply           Execute cleanup (default: dry-run only)
  --dry-run         Report only, make no changes (default)
  --json            Machine-readable JSON output
  --stale-days N    Age threshold in days for stale classification (default: 7)
  -h, --help        Show this help

Classification:
  receipt-found       Has a matching receipt in t0_receipts.ndjson
                      Action: move to completed/
  stale-no-receipt    No receipt, older than --stale-days threshold
                      Action: move to abandoned/
  recent-no-receipt   No receipt, younger than threshold
                      Action: skip (not stale yet)
  empty               Missing both dispatch-spec.json and instruction.md
                      Action: error (manual review needed)
  error               Read/classify failure
                      Action: error (manual review needed)

Safety:
  - Dry-run is the DEFAULT. Nothing is moved without --apply.
  - Bundles with a receipt go to completed/, not abandoned/.
  - Recent bundles (within --stale-days) are never moved.
  - All bundles remain on disk — nothing is deleted.

Examples:
  vnx dispatch-cleanup                     # dry-run: see what would happen
  vnx dispatch-cleanup --apply             # actually move bundles
  vnx dispatch-cleanup --json              # machine-readable dry-run
  vnx dispatch-cleanup --stale-days 14     # only move bundles 14+ days old
HELP
        return 0
        ;;
      -*)
        err "[dispatch-cleanup] Unknown option: $1"
        return 1
        ;;
      *)
        err "[dispatch-cleanup] Unexpected argument: $1"
        return 1
        ;;
    esac
  done

  # Resolve the Python script
  local cleanup_py="$VNX_HOME/scripts/lib/dispatch_cleanup.py"
  if [ ! -f "$cleanup_py" ]; then
    err "[dispatch-cleanup] Python module not found: $cleanup_py"
    return 1
  fi

  # Resolve the Python interpreter
  local python_bin="${VNX_PYTHON:-python3}"
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    err "[dispatch-cleanup] Python interpreter not found: $python_bin"
    return 1
  fi

  local lib_dir="$VNX_HOME/scripts/lib"

  local -a py_args=("$cleanup_py")
  if [ "$apply" -eq 1 ]; then
    py_args+=("--apply")
  fi
  if [ "$json_output" -eq 1 ]; then
    py_args+=("--json")
  fi
  py_args+=("--stale-days" "$stale_days")

  PYTHONPATH="$lib_dir:${PYTHONPATH:-}" "$python_bin" "${py_args[@]}"
  local rc=$?

  if [ "$apply" -eq 1 ] && [ $rc -eq 0 ]; then
    log "[dispatch-cleanup] Cleanup complete."
  fi

  return $rc
}
