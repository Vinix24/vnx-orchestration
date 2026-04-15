#!/usr/bin/env bash
# VNX Overnight Chain 2 — F54-F57
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_DIR=".vnx-data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/overnight_chain_2.log"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
BRANCH="feat/f46-f50-intelligence-loop-dashboard"

DISPATCHES=(
  "20260413-220000-f54-pr1-temporal-patterns-A|backend-developer|F54-PR1 temporal patterns"
  "20260413-220100-f55-pr1-treesitter-graph-A|backend-developer|F55-PR1 repo map"
  "20260413-220200-f55-pr2-dispatch-integration-A|backend-developer|F55-PR2 dispatch enricher"
  "20260413-220300-f56-pr1-memory-consolidation-A|backend-developer|F56-PR1 memory consolidation"
  "20260413-220400-f57-pr1-karpathy-loop-A|backend-developer|F57-PR1 Karpathy loop"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

auto_commit() {
  local gate="$1"
  local changes
  changes=$(git status --porcelain 2>/dev/null | grep -v "^??" || true)
  if [ -n "$changes" ]; then
    log "Auto-committing for $gate"
    git add -A -- scripts/ tests/ dashboard/ .vnx/ 2>/dev/null || true
    git commit -m "feat($gate): auto-commit from overnight chain

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>" 2>/dev/null || true
  fi
}

wait_for_receipt() {
  local dispatch_id="$1"
  local timeout=600
  local elapsed=0
  while [ $elapsed -lt $timeout ]; do
    if tail -10 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
      local status
      status=$(tail -10 "$RECEIPTS" | grep "$dispatch_id" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status','unknown'))" 2>/dev/null || echo "unknown")
      log "Receipt: $dispatch_id status=$status"
      return 0
    fi
    sleep 15
    elapsed=$((elapsed + 15))
  done
  log "TIMEOUT: $dispatch_id"
  return 1
}

log "═══════════════════════════════════════════════════"
log "Overnight Chain 2 (F54-F57) — $(date)"
log "═══════════════════════════════════════════════════"

success=0
failed=0

for entry in "${DISPATCHES[@]}"; do
  IFS='|' read -r dispatch_id role description <<< "$entry"
  dispatch_file=".vnx-data/dispatches/pending/${dispatch_id}.md"

  log "── $description ──"

  # Skip completed
  if tail -20 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
    log "SKIP: already completed"
    success=$((success + 1))
    continue
  fi

  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: file not found"
    failed=$((failed + 1))
    continue
  fi

  log "Dispatching $dispatch_id..."
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id T1 \
    --dispatch-id "$dispatch_id" \
    --model sonnet \
    --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" || true

  if wait_for_receipt "$dispatch_id"; then
    gate=$(echo "$dispatch_id" | sed 's/.*-\(f[0-9]*-pr[0-9]*\)-.*/\1/')
    auto_commit "$gate"
    success=$((success + 1))
    log "✓ $description DONE"
  else
    log "✗ $description FAILED"
    failed=$((failed + 1))
  fi
done

log ""
log "Pushing..."
git push origin "$BRANCH" 2>>"$LOG" || log "Push failed"

log "═══════════════════════════════════════════════════"
log "COMPLETE — Success: $success/${#DISPATCHES[@]} Failed: $failed"
log "═══════════════════════════════════════════════════"
git log --oneline -8 >> "$LOG"
