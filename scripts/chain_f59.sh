#!/usr/bin/env bash
# VNX Chain — F59 Real Intelligence Extraction
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG=".vnx-data/logs/chain_f59.log"
mkdir -p "$(dirname "$LOG")"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
BRANCH="feat/f46-f50-intelligence-loop-dashboard"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

auto_commit() {
  local gate="$1"
  local changes
  changes=$(git status --porcelain 2>/dev/null | grep -v "^??" || true)
  if [ -n "$changes" ]; then
    log "Auto-committing for $gate"
    git add -A -- scripts/ tests/ dashboard/ .vnx/ 2>/dev/null || true
    git commit -m "feat($gate): auto-commit from chain

Dispatch-ID: $2

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>" 2>/dev/null || true
  fi
}

wait_for_receipt() {
  local dispatch_id="$1"
  local elapsed=0
  while [ $elapsed -lt 600 ]; do
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

dispatch_and_wait() {
  local dispatch_id="$1" role="$2" description="$3" terminal="${4:-T1}"
  local dispatch_file=".vnx-data/dispatches/pending/${dispatch_id}.md"

  log "── $description ──"

  if tail -20 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
    log "SKIP: already completed"; return 0
  fi
  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: file not found"; return 1
  fi

  log "Dispatching to $terminal..."
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id "$terminal" --dispatch-id "$dispatch_id" --model sonnet --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" || true

  if wait_for_receipt "$dispatch_id"; then
    local gate
    gate=$(echo "$dispatch_id" | sed 's/.*-\(f[0-9]*-pr[0-9]*\)-.*/\1/')
    auto_commit "$gate" "$dispatch_id"
    log "✓ $description DONE"
    return 0
  else
    log "✗ $description FAILED"
    return 1
  fi
}

log "═══════════════════════════════════════════════════"
log "F59 Chain — Real Intelligence Extraction — $(date)"
log "═══════════════════════════════════════════════════"

# PR1-PR3 sequential on T1
dispatch_and_wait "20260414-120000-f59-pr1-event-analyzer-A" "backend-developer" "F59-PR1 event analyzer" "T1"
dispatch_and_wait "20260414-120100-f59-pr2-pattern-extractor-A" "backend-developer" "F59-PR2 pattern extractor" "T1"
dispatch_and_wait "20260414-120200-f59-pr3-pipeline-integration-A" "backend-developer" "F59-PR3 pipeline + API" "T1"

# PR4 on T3 (frontend, needs PR3 APIs)
dispatch_and_wait "20260414-120300-f59-pr4-dashboard-viewer-C" "frontend-developer" "F59-PR4 dashboard viewer" "T3"

# Push
log "Pushing..."
git push origin "$BRANCH" 2>>"$LOG" || log "Push failed"

log "═══════════════════════════════════════════════════"
log "F59 COMPLETE — $(date)"
log "═══════════════════════════════════════════════════"
git log --oneline -6 >> "$LOG"
