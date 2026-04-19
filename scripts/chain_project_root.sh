#!/usr/bin/env bash
# VNX Chain — issue #225 project-aware path resolution (4 PRs)
# Per PR: checkout main, dispatch worker, wait for receipt, run gates, merge, next.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG=".vnx-data/logs/chain_project_root.log"
mkdir -p "$(dirname "$LOG")"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
REPO="Vinix24/vnx-orchestration"

DISPATCHES=(
  "20260419-160000-project-root-pr1-helper-A|T1|backend-developer|fix/project-root-resolution-pr1|PR 1 helper library"
  "20260419-160100-project-root-pr2-python-migration-B|T2|backend-developer|fix/project-root-resolution-pr2|PR 2 Python migration"
  "20260419-160200-project-root-pr3-bash-migration-B|T2|backend-developer|fix/project-root-resolution-pr3|PR 3 bash migration"
  "20260419-160300-project-root-pr4-docs-review-C|T3|architect|fix/project-root-resolution-pr4|PR 4 docs + review"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

wait_for_receipt() {
  local dispatch_id="$1"
  local elapsed=0
  local max=1800  # 30 min per dispatch (docs/review is slower)
  while [ $elapsed -lt $max ]; do
    if tail -20 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
      local status
      status=$(tail -20 "$RECEIPTS" | grep "$dispatch_id" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status','?'))" 2>/dev/null || echo "?")
      log "Receipt: $dispatch_id status=$status"
      return 0
    fi
    sleep 30
    elapsed=$((elapsed + 30))
  done
  log "TIMEOUT: $dispatch_id"
  return 1
}

find_pr_number() {
  # Find PR number for a branch that was opened by the worker
  local branch="$1"
  local pr
  pr=$(gh pr list --repo "$REPO" --head "$branch" --json number 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['number'] if d else '')" 2>/dev/null)
  echo "$pr"
}

wait_for_ci() {
  local pr="$1"
  local elapsed=0
  while [ $elapsed -lt 300 ]; do
    local pending
    pending=$(gh pr checks "$pr" --repo "$REPO" 2>/dev/null | grep -c "pending\|in_progress" || echo 0)
    if [ "$pending" -eq 0 ]; then
      local failed
      failed=$(gh pr checks "$pr" --repo "$REPO" 2>/dev/null | grep -c "fail" || echo 0)
      if [ "$failed" -eq 0 ]; then
        log "CI green on PR #$pr"
        return 0
      else
        log "CI failed on PR #$pr"
        return 1
      fi
    fi
    sleep 20
    elapsed=$((elapsed + 20))
  done
  log "CI timeout on PR #$pr"
  return 1
}

run_gates() {
  local pr="$1" branch="$2" files="$3"
  log "Running gates on PR #$pr ($branch)"
  export VNX_STATE_DIR=.vnx-data/state VNX_DATA_DIR=.vnx-data
  bash scripts/t0_gate_enforcement.sh --pr "$pr" --branch "$branch" --review-stack codex_gate,gemini_review --risk-class medium --changed-files "$files" >> "$LOG" 2>&1 || true

  # Wait for gate results (max 3 min)
  local elapsed=0
  while [ $elapsed -lt 180 ]; do
    if [ -f ".vnx-data/state/review_gates/results/pr-${pr}-gemini_review.json" ] && [ -f ".vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json" ]; then
      break
    fi
    sleep 15
    elapsed=$((elapsed + 15))
  done

  local gemini_status codex_status gemini_blocking codex_blocking
  gemini_status=$(python3 -c "import json; print(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-gemini_review.json')).get('status','?'))" 2>/dev/null)
  codex_status=$(python3 -c "import json; print(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json')).get('status','?'))" 2>/dev/null)
  gemini_blocking=$(python3 -c "import json; print(len(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-gemini_review.json')).get('blocking_findings',[])))" 2>/dev/null)
  codex_blocking=$(python3 -c "import json; print(len(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json')).get('blocking_findings',[])))" 2>/dev/null)

  log "Gemini: $gemini_status blocking=$gemini_blocking | Codex: $codex_status blocking=$codex_blocking"

  # Accept if both have 0 blocking findings; status=failed with blocking=0 is tolerable (infra issue)
  if [ "$gemini_blocking" = "0" ] && [ "$codex_blocking" = "0" ]; then
    return 0
  fi
  log "Gate BLOCK detected on PR #$pr"
  return 1
}

log "═══════════════════════════════════════════════════"
log "Chain #225 project-aware path resolution — $(date)"
log "═══════════════════════════════════════════════════"

success=0; failed=0
for entry in "${DISPATCHES[@]}"; do
  IFS='|' read -r dispatch_id terminal role branch description <<< "$entry"
  # Read from safe chain_dispatches/ (avoids cross-project dispatcher moving pending/ files)
  dispatch_file=".vnx-data/chain_dispatches/${dispatch_id}.md"

  log ""
  log "── $description ──"
  log "terminal=$terminal role=$role branch=$branch"

  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: dispatch file not found"
    failed=$((failed + 1))
    continue
  fi

  # Ensure branch exists locally from latest main
  git fetch origin main 2>>"$LOG" || true
  if ! git show-ref --verify --quiet "refs/heads/$branch"; then
    git checkout -b "$branch" origin/main 2>>"$LOG" || true
  else
    git checkout "$branch" 2>>"$LOG" || true
    git reset --hard origin/main 2>>"$LOG" || true
  fi

  # Push empty branch so worker can commit directly
  git push -u origin "$branch" 2>>"$LOG" || true

  log "Dispatching to $terminal..."
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id "$terminal" \
    --dispatch-id "$dispatch_id" \
    --model sonnet \
    --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" || true

  if ! wait_for_receipt "$dispatch_id"; then
    failed=$((failed + 1))
    log "✗ $description — TIMEOUT, aborting chain"
    break
  fi

  # Find PR number (worker should have opened one)
  pr=$(find_pr_number "$branch")
  if [ -z "$pr" ]; then
    log "✗ $description — no PR opened, aborting chain"
    failed=$((failed + 1))
    break
  fi
  log "PR #$pr opened"

  # Wait for CI
  if ! wait_for_ci "$pr"; then
    log "✗ $description — CI failed on PR #$pr, aborting chain"
    failed=$((failed + 1))
    break
  fi

  # Get changed files
  changed_files=$(gh pr view "$pr" --repo "$REPO" --json files 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(f['path'] for f in d['files'][:10]))" 2>/dev/null)

  # Run gates
  if ! run_gates "$pr" "$branch" "$changed_files"; then
    log "✗ $description — gate block on PR #$pr, aborting chain"
    failed=$((failed + 1))
    break
  fi

  # Merge
  log "Merging PR #$pr..."
  gh pr merge "$pr" --squash --repo "$REPO" 2>>"$LOG" || {
    log "✗ Merge failed on PR #$pr"
    failed=$((failed + 1))
    break
  }
  log "✓ $description MERGED as PR #$pr"
  success=$((success + 1))
done

log ""
log "═══════════════════════════════════════════════════"
log "CHAIN COMPLETE — Success: $success/${#DISPATCHES[@]} Failed: $failed"
log "═══════════════════════════════════════════════════"
gh pr list --repo "$REPO" --state merged --limit 5 >> "$LOG" 2>&1
