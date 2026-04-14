#!/usr/bin/env bash
# VNX Overnight Chain Runner
# Runs dispatches sequentially on T1, auto-commits, pushes at end.
set -uo pipefail  # no -e: don't exit on errors, handle them per-dispatch

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_DIR=".vnx-data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/overnight_chain.log"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
BRANCH="feat/f46-f50-intelligence-loop-dashboard"

# Dispatch list — in execution order
DISPATCHES=(
  "20260413-170000-f52-pr1-cli-commands-A|backend-developer|F52-PR1 CLI commands"
  "20260413-170100-f52-pr2-permissions-A|backend-developer|F52-PR2 permissions"
  "20260413-170200-f52-pr3-commit-enforce-A|backend-developer|F52-PR3 commit enforce"
  "20260413-170300-f53-pr1-adapter-base-A|backend-developer|F53-PR1 adapter base"
  "20260413-170400-f53-pr2-gemini-codex-A|backend-developer|F53-PR2 gemini+codex"
  "20260413-170500-f53-pr3-ollama-routing-A|backend-developer|F53-PR3 ollama routing"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

auto_commit() {
  local gate="$1"
  local changes
  changes=$(git status --porcelain | grep -v "^??" | head -20)
  if [ -n "$changes" ]; then
    log "Auto-committing uncommitted changes for $gate"
    git add -A -- scripts/ tests/ dashboard/ .vnx/ 2>/dev/null || true
    git commit -m "feat($gate): auto-commit from overnight chain

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>" 2>/dev/null || true
    log "Committed."
  else
    log "Working tree clean — no auto-commit needed."
  fi
}

wait_for_receipt() {
  local dispatch_id="$1"
  local timeout=600  # 10 min max per dispatch
  local elapsed=0
  while [ $elapsed -lt $timeout ]; do
    if tail -10 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
      local status
      status=$(tail -10 "$RECEIPTS" | grep "$dispatch_id" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status','unknown'))" 2>/dev/null || echo "unknown")
      log "Receipt: dispatch=$dispatch_id status=$status"
      return 0
    fi
    sleep 15
    elapsed=$((elapsed + 15))
  done
  log "TIMEOUT waiting for receipt: $dispatch_id (${timeout}s)"
  return 1
}

# ── Main ──────────────────────────────────────────────────
log "═══════════════════════════════════════════════════"
log "VNX Overnight Chain — $(date)"
log "Branch: $BRANCH"
log "Dispatches: ${#DISPATCHES[@]}"
log "═══════════════════════════════════════════════════"

success=0
failed=0

for entry in "${DISPATCHES[@]}"; do
  IFS='|' read -r dispatch_id role description <<< "$entry"
  dispatch_file=".vnx-data/dispatches/pending/${dispatch_id}.md"

  log ""
  log "── Dispatch: $description ──"

  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: dispatch file not found: $dispatch_file"
    failed=$((failed + 1))
    continue
  fi

  # Skip already-completed dispatches (receipt exists)
  if tail -20 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
    log "SKIP: already completed (receipt exists): $dispatch_id"
    success=$((success + 1))
    continue
  fi

  # Check T1 availability
  t1_check=$(python3 scripts/runtime_core_cli.py check-terminal --terminal T1 --dispatch-id "$dispatch_id" 2>/dev/null || echo '{"available":false}')
  if ! echo "$t1_check" | python3 -c "import sys,json;assert json.load(sys.stdin).get('available')" 2>/dev/null; then
    log "SKIP: T1 not available — $t1_check"
    failed=$((failed + 1))
    continue
  fi

  # Dispatch
  log "Dispatching $dispatch_id to T1..."
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id T1 \
    --dispatch-id "$dispatch_id" \
    --model sonnet \
    --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" &
  dispatch_pid=$!

  # Wait for subprocess to finish
  wait $dispatch_pid 2>/dev/null || true

  # Wait for receipt
  if wait_for_receipt "$dispatch_id"; then
    # Auto-commit if needed
    gate=$(echo "$dispatch_id" | sed 's/.*-\(f[0-9]*-pr[0-9]*\)-.*/\1/')
    auto_commit "$gate"
    success=$((success + 1))
    log "✓ $description — DONE"
  else
    log "✗ $description — FAILED (timeout or no receipt)"
    failed=$((failed + 1))
    # Continue to next dispatch
  fi
done

# Push all commits
log ""
log "── Pushing all commits ──"
git push origin "$BRANCH" 2>>"$LOG" || log "Push failed (non-fatal)"

# Summary
log ""
log "═══════════════════════════════════════════════════"
log "CHAIN COMPLETE — $(date)"
log "Success: $success / ${#DISPATCHES[@]}"
log "Failed:  $failed / ${#DISPATCHES[@]}"
log "═══════════════════════════════════════════════════"
log "Commits:"
git log --oneline -10 | tee -a "$LOG"
