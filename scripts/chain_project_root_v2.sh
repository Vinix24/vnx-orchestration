#!/usr/bin/env bash
# Chain runner v2 — for remaining PRs 2/3/4 of issue #225
# After worker finishes: auto-push local commits, auto-open PR if missing,
# then run gates + merge.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG=".vnx-data/logs/chain_project_root_v2.log"
mkdir -p "$(dirname "$LOG")"
RECEIPTS=".vnx-data/state/t0_receipts.ndjson"
REPO="Vinix24/vnx-orchestration"

# Remaining PRs only (PR 1 = #226, already opened)
DISPATCHES=(
  "20260419-160100-project-root-pr2-python-migration-B|T2|backend-developer|fix/project-root-resolution-pr2|PR 2 Python migration|refactor(path-resolution): migrate Python scripts (#225 PR 2/4)"
  "20260419-160200-project-root-pr3-bash-migration-B|T2|backend-developer|fix/project-root-resolution-pr3|PR 3 bash migration|refactor(path-resolution): migrate bash scripts (#225 PR 3/4)"
  "20260419-160300-project-root-pr4-docs-review-C|T3|architect|fix/project-root-resolution-pr4|PR 4 docs + review|docs: project-root resolution — README + migration guide + review (#225 PR 4/4)"
)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

wait_for_receipt() {
  local dispatch_id="$1"
  local elapsed=0
  while [ $elapsed -lt 1800 ]; do
    if tail -30 "$RECEIPTS" 2>/dev/null | grep -q "$dispatch_id"; then
      return 0
    fi
    sleep 30
    elapsed=$((elapsed + 30))
  done
  return 1
}

wait_for_ci_and_gates() {
  local pr="$1"
  local elapsed=0
  while [ $elapsed -lt 300 ]; do
    local pending failed
    pending=$(gh pr checks "$pr" --repo "$REPO" 2>/dev/null | grep -c "pending\|in_progress" || echo 0)
    if [ "$pending" -eq 0 ]; then
      failed=$(gh pr checks "$pr" --repo "$REPO" 2>/dev/null | grep -c "fail" || echo 0)
      [ "$failed" -eq 0 ] && return 0 || return 1
    fi
    sleep 20; elapsed=$((elapsed + 20))
  done
  return 1
}

ensure_pr_open() {
  # After worker finishes: check if branch has commits, push them, open PR if missing
  local branch="$1" title="$2"
  git fetch origin "$branch" 2>>"$LOG" || true
  local local_sha remote_sha
  local_sha=$(git rev-parse "$branch" 2>/dev/null)
  remote_sha=$(git rev-parse "origin/$branch" 2>/dev/null || echo "")

  if [ -z "$remote_sha" ] || [ "$local_sha" != "$remote_sha" ]; then
    log "Pushing $branch ($local_sha)"
    git push origin "$branch" 2>>"$LOG" || log "push failed for $branch"
  fi

  local pr
  pr=$(gh pr list --repo "$REPO" --head "$branch" --state open --json number 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['number'] if d else '')" 2>/dev/null)
  if [ -z "$pr" ]; then
    log "Opening PR for $branch"
    pr=$(gh pr create --repo "$REPO" --base main --head "$branch" --title "$title" --body "Automated dispatch result from chain_project_root_v2.sh. See issue #225." 2>&1 | grep -oE "pull/[0-9]+" | cut -d/ -f2 | head -1)
    [ -n "$pr" ] && log "Opened PR #$pr"
  fi
  echo "$pr"
}

run_gates() {
  local pr="$1" branch="$2" files="$3"
  export VNX_STATE_DIR=.vnx-data/state VNX_DATA_DIR=.vnx-data
  bash scripts/t0_gate_enforcement.sh --pr "$pr" --branch "$branch" --review-stack codex_gate,gemini_review --risk-class medium --changed-files "$files" >> "$LOG" 2>&1 || true
  local elapsed=0
  while [ $elapsed -lt 300 ]; do
    [ -f ".vnx-data/state/review_gates/results/pr-${pr}-gemini_review.json" ] && [ -f ".vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json" ] && break
    sleep 20; elapsed=$((elapsed + 20))
  done
  local gb cb
  gb=$(python3 -c "import json; print(len(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-gemini_review.json')).get('blocking_findings',[])))" 2>/dev/null || echo "-1")
  cb=$(python3 -c "import json; print(len(json.load(open('.vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json')).get('blocking_findings',[])))" 2>/dev/null || echo "-1")
  log "Gates PR #$pr: gemini_blocking=$gb codex_blocking=$cb"
  [ "$gb" = "0" ] && [ "$cb" = "0" ] && return 0
  return 1
}

log "═══════════════════════════════════════════════════"
log "Chain v2 #225 — PRs 2/3/4 — $(date)"
log "═══════════════════════════════════════════════════"

success=0; failed=0
for entry in "${DISPATCHES[@]}"; do
  IFS='|' read -r dispatch_id terminal role branch description pr_title <<< "$entry"
  dispatch_file=".vnx-data/chain_dispatches/${dispatch_id}.md"

  log ""
  log "── $description ──"

  if [ ! -f "$dispatch_file" ]; then
    log "SKIP: $dispatch_file not found"; failed=$((failed + 1)); continue
  fi

  # Create/reset branch from latest main
  git fetch origin main 2>>"$LOG" || true
  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git checkout "$branch" 2>>"$LOG" || true
    git reset --hard origin/main 2>>"$LOG" || true
  else
    git checkout -b "$branch" origin/main 2>>"$LOG" || true
  fi
  git push -u origin "$branch" --force 2>>"$LOG" || true

  log "Dispatching to $terminal (role=$role)"
  python3 scripts/lib/subprocess_dispatch.py \
    --terminal-id "$terminal" \
    --dispatch-id "$dispatch_id" \
    --model sonnet \
    --role "$role" \
    --instruction "$(cat "$dispatch_file")" 2>>"$LOG" || true

  if ! wait_for_receipt "$dispatch_id"; then
    log "✗ $description — receipt timeout"; failed=$((failed + 1)); break
  fi

  # Ensure PR exists (push + create if worker didn't)
  pr=$(ensure_pr_open "$branch" "$pr_title")
  if [ -z "$pr" ]; then
    log "✗ $description — could not open PR (no commits?)"; failed=$((failed + 1)); break
  fi
  log "PR #$pr ready"

  # CI
  if ! wait_for_ci_and_gates "$pr"; then
    log "✗ $description — CI failed on #$pr"; failed=$((failed + 1)); break
  fi

  # Gates
  changed=$(gh pr view "$pr" --repo "$REPO" --json files 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(','.join(f['path'] for f in d['files'][:10]))" 2>/dev/null)
  if ! run_gates "$pr" "$branch" "$changed"; then
    log "✗ $description — gate block on #$pr"; failed=$((failed + 1)); break
  fi

  # Merge
  gh pr merge "$pr" --squash --repo "$REPO" 2>>"$LOG" || { log "✗ merge failed"; failed=$((failed + 1)); break; }
  log "✓ $description MERGED as PR #$pr"
  success=$((success + 1))
done

log ""
log "═══════════════════════════════════════════════════"
log "CHAIN v2 COMPLETE — Success: $success/${#DISPATCHES[@]} Failed: $failed"
log "═══════════════════════════════════════════════════"
