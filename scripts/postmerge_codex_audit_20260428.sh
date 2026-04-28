#!/usr/bin/env bash
# Post-merge codex audit for PRs #279-#285 (gemerged 2026-04-28 zonder final codex)
# Tracks against OI-1181. Files new OIs for blocking findings; closes OI-1181 if all clean.
set -euo pipefail
cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
export VNX_DATA_DIR=$PWD/.vnx-data
export VNX_STATE_DIR=$PWD/.vnx-data/state
export VNX_CODEX_HEADLESS_MODEL=gpt-5.4

REPORT=.vnx-data/unified_reports/20260428-postmerge-codex-audit.md
mkdir -p $(dirname "$REPORT")
{
  echo "# Post-merge Codex Audit — 2026-04-28"
  echo ""
  echo "**Audit subject**: 7 PRs merged 2026-04-28 with gemini+CI gates only (codex usage-limit hit)."
  echo "**Started**: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""
  echo "## Per-PR results"
  echo ""
} > "$REPORT"

# bash 3.2 compatible: parallel arrays via paired strings
PR_LIST="279 280 281 282 283 284 285"
ALL_CLEAN=true

branch_for_pr() {
  case "$1" in
    279) echo "feat/state-rebuild-trigger" ;;
    280) echo "feat/build-t0-state-register-reader" ;;
    281) echo "feat/append-receipt-codex-register-emit" ;;
    282) echo "feat/gate-artifacts-codex-emit" ;;
    283) echo "feat/build-t0-state-register-canonical" ;;
    284) echo "feat/t0-state-index-detail-split" ;;
    285) echo "feat/project-status-md-generator" ;;
    *) echo "unknown" ;;
  esac
}

for pr in $PR_LIST; do
  branch=$(branch_for_pr "$pr")
  echo "### PR #$pr ($branch)" >> "$REPORT"
  rm -f .vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json
  python3 scripts/review_gate_manager.py request-and-execute --pr $pr --branch main --review-stack codex_gate --risk-class low --mode final --dispatch-id 20260428-pr${pr}-postmerge-audit --json > /tmp/audit-pr${pr}.json 2>&1 || true
  RESULT=.vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json
  if [ -f "$RESULT" ]; then
    BL=$(python3 -c "import json; d=json.load(open('$RESULT')); print(len(d.get('blocking_findings',[])))")
    AD=$(python3 -c "import json; d=json.load(open('$RESULT')); print(len(d.get('advisory_findings',[])))")
    DUR=$(python3 -c "import json; d=json.load(open('$RESULT')); print(f\"{d.get('duration_seconds',0):.1f}s\")")
    echo "  - Status: blocking=$BL advisory=$AD duration=$DUR" >> "$REPORT"
    if [ "$BL" -gt 0 ]; then
      ALL_CLEAN=false
      python3 -c "
import json
d = json.load(open('$RESULT'))
for f in d.get('blocking_findings',[]):
    print('  - BLOCKING: ' + f.get('message','')[:300].replace(chr(10),' '))
" >> "$REPORT"
      python3 scripts/open_items_manager.py add --dispatch 20260428-pr${pr}-postmerge-audit --severity warn --title "Codex post-merge finding on PR #$pr" --pr $pr --details "Found by post-merge codex audit at $(date -u). See $RESULT for details." --report "$REPORT" 2>&1 | tail -1 >> "$REPORT" || true
    fi
  else
    echo "  - Codex gate FAILED to produce result file" >> "$REPORT"
    ALL_CLEAN=false
  fi
  echo "" >> "$REPORT"
done

echo "## Summary" >> "$REPORT"
if [ "$ALL_CLEAN" = "true" ]; then
  echo "All 7 PRs codex-clean ✅" >> "$REPORT"
  python3 scripts/open_items_manager.py close OI-1181 --reason "Post-merge codex audit clean — all 7 PRs (#279-#285) pass codex gate with no blocking findings. Audit report: $REPORT" 2>&1 | tail -1 >> "$REPORT" || true
else
  echo "Blocking findings detected — see per-PR section + new OIs filed" >> "$REPORT"
fi
echo "**Completed**: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$REPORT"
echo "AUDIT COMPLETE — see $REPORT"
