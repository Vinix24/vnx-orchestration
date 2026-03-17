#!/bin/bash
# Merge worktree intelligence data back to main repo
# Usage: vnx_worktree_merge_data.sh <worktree-.vnx-data-path>

set -euo pipefail
source "$(dirname "$0")/lib/vnx_paths.sh"

WT_DATA="${1:?Usage: $0 <worktree-.vnx-data-path>}"
MAIN_DATA="$VNX_DATA_DIR"

echo "Merging intelligence from $WT_DATA → $MAIN_DATA"

# 1. Reports (unique timestamp filenames, no conflicts)
if [ -d "$WT_DATA/unified_reports" ]; then
  count=$(ls "$WT_DATA/unified_reports/"*.md 2>/dev/null | wc -l)
  cp -n "$WT_DATA/unified_reports/"*.md "$MAIN_DATA/unified_reports/" 2>/dev/null || true
  echo "[ok] Reports: $count files merged"
fi

# 2. Receipts (append NDJSON, dedup on receipt_id)
if [ -f "$WT_DATA/state/t0_receipts.ndjson" ]; then
  comm -23 \
    <(jq -r '.receipt_id' "$WT_DATA/state/t0_receipts.ndjson" 2>/dev/null | sort -u) \
    <(jq -r '.receipt_id' "$MAIN_DATA/state/t0_receipts.ndjson" 2>/dev/null | sort -u) \
  | while read -r rid; do
      grep "\"receipt_id\":\"$rid\"" "$WT_DATA/state/t0_receipts.ndjson" >> "$MAIN_DATA/state/t0_receipts.ndjson"
    done
  echo "[ok] Receipts: merged (deduplicated)"
fi

# 3. Intelligence DB (INSERT OR IGNORE — preserves existing main data)
if [ -f "$WT_DATA/database/intelligence.db" ] && [ -f "$MAIN_DATA/database/intelligence.db" ]; then
  sqlite3 "$WT_DATA/database/intelligence.db" ".dump" | \
    sed 's/INSERT/INSERT OR IGNORE/g' | \
    sqlite3 "$MAIN_DATA/database/intelligence.db" 2>/dev/null || true
  echo "[ok] Intelligence DB: merged"
fi

echo "Done. Worktree intelligence now available in main repo."
