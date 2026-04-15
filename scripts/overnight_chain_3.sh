#!/usr/bin/env bash
# VNX Chain 3 — F58 Observability + Layered Prompts
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG=".vnx-data/logs/chain_3_f58.log"
mkdir -p "$(dirname "$LOG")"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
BRANCH="feat/f46-f50-intelligence-loop-dashboard"

DISPATCHES=(
  "20260414-090000-f58-pr1-manifest-session-A|backend-developer|F58-PR1 manifest+session+commit"
  "20260414-090100-f58-pr2-event-archive-A|backend-developer|F58-PR2 event archive+audit linkage"
  "20260414-090200-f58-pr3-layered-prompt-A|backend-developer|F58-PR3 layered user message architecture"
  "20260414-090300-f58-pr4-trace-verification-B|test-engineer|F58-PR4 E2E trace verification tests"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

auto_commit() {
  local gate="$1"
  local changes
  changes=$(git status --porcelain 2>/dev/null | grep -v "^??" || true)
  if [ -n "$changes" ]; then
    log "Auto-committing for $gate"
    git add -A -- scripts/ tests/ dashboard/ .vnx/ 2>/dev/null || true
    git commit -m "feat($gate): auto-commit from chain

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

log "═══════════════════════════════════════════════════"
log "Chain 3 (F58) — $(date)"
log "═══════════════════════════════════════════════════"

success=0; failed=0

for entry in "${DISPATCHES[@]}"; do
  IFS='|' read -r dispatch_id role description <<< "$entry"
  dispatch_file=".vnx-data/dispatches/pending/${dispatch_id}.md"

  log "── $description ──"

  if tail -20 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
    log "SKIP: already completed"; success=$((success + 1)); continue
  fi

  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: file not found"; failed=$((failed + 1)); continue
  fi

  log "Dispatching..."
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id T1 --dispatch-id "$dispatch_id" --model sonnet --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" || true

  if wait_for_receipt "$dispatch_id"; then
    gate=$(echo "$dispatch_id" | sed 's/.*-\(f[0-9]*-pr[0-9]*\)-.*/\1/')
    auto_commit "$gate"
    success=$((success + 1)); log "✓ $description DONE"
  else
    failed=$((failed + 1)); log "✗ $description FAILED"
  fi
done

log "Pushing..."
git push origin "$BRANCH" 2>>"$LOG" || log "Push failed"

log "═══════════════════════════════════════════════════"
log "COMPLETE — Success: $success/${#DISPATCHES[@]} Failed: $failed"
log "═══════════════════════════════════════════════════"
git log --oneline -5 >> "$LOG"
