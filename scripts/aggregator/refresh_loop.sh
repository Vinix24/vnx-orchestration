#!/usr/bin/env bash
# Refresh loop wrapper for the VNX read-only federation aggregator.
# Invoked by launchd (com.vnx.aggregator.plist) on a 60s schedule.
#
# READ-ONLY: this script attaches every source DB in `?mode=ro` and
# materializes a unified view at $AGG_DIR/data.db. The operator can
# `rm -rf $AGG_DIR` at any point with zero data loss.

set -euo pipefail

REPO_ROOT="${VNX_AGGREGATOR_REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
AGG_DIR="${VNX_AGGREGATOR_DIR:-$HOME/.vnx-aggregator}"
LOG_DIR="$AGG_DIR/logs"
LOG_FILE="$LOG_DIR/refresh.log"

mkdir -p "$AGG_DIR" "$LOG_DIR"

cd "$REPO_ROOT"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[$ts] refresh start" >> "$LOG_FILE"

if python3 -m scripts.aggregator.build_central_view >> "$LOG_FILE" 2>&1; then
  ts_done="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts_done] refresh ok" >> "$LOG_FILE"
  exit 0
else
  rc=$?
  ts_err="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts_err] refresh failed rc=$rc" >> "$LOG_FILE"
  exit "$rc"
fi
