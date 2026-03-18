#!/bin/bash
# Merge worktree intelligence data back to main repo
# Usage: vnx_worktree_merge_data.sh <worktree-.vnx-data-path>

set -euo pipefail
source "$(dirname "$0")/lib/vnx_paths.sh"

WT_DATA="${1:?Usage: $0 <worktree-.vnx-data-path>}"
MAIN_DATA="$VNX_DATA_DIR"

# Derive subdirectories using VNX_*_DIR conventions
WT_STATE_DIR="$WT_DATA/$(basename "$VNX_STATE_DIR")"
WT_REPORTS_DIR="$WT_DATA/$(basename "$VNX_REPORTS_DIR")"
WT_DB_DIR="$WT_DATA/$(basename "$VNX_DB_DIR")"

echo "Merging intelligence from $WT_DATA → $MAIN_DATA"

# 1. Reports (unique timestamp filenames, no conflicts)
if [ -d "$WT_REPORTS_DIR" ]; then
  count=$(ls "$WT_REPORTS_DIR/"*.md 2>/dev/null | wc -l)
  cp -n "$WT_REPORTS_DIR/"*.md "$VNX_REPORTS_DIR/" 2>/dev/null || true
  echo "[ok] Reports: $count files merged"
fi

# 2. Receipts (append NDJSON, dedup on receipt_id)
WT_RECEIPTS="$WT_STATE_DIR/t0_receipts.ndjson"
MAIN_RECEIPTS="$VNX_STATE_DIR/t0_receipts.ndjson"
if [ -f "$WT_RECEIPTS" ]; then
  comm -23 \
    <(jq -r '.receipt_id' "$WT_RECEIPTS" 2>/dev/null | sort -u) \
    <(jq -r '.receipt_id' "$MAIN_RECEIPTS" 2>/dev/null | sort -u) \
  | while read -r rid; do
      grep "\"receipt_id\":\"$rid\"" "$WT_RECEIPTS" >> "$MAIN_RECEIPTS"
    done
  echo "[ok] Receipts: merged (deduplicated)"
fi

# 3. Intelligence DB (INSERT OR IGNORE — preserves existing main data)
if [ -f "$WT_DB_DIR/intelligence.db" ] && [ -f "$VNX_DB_DIR/intelligence.db" ]; then
  sqlite3 "$WT_DB_DIR/intelligence.db" ".dump" | \
    sed 's/INSERT/INSERT OR IGNORE/g' | \
    sqlite3 "$VNX_DB_DIR/intelligence.db" 2>/dev/null || true
  echo "[ok] Intelligence DB: merged"
fi

echo "Done. Worktree intelligence now available in main repo."
