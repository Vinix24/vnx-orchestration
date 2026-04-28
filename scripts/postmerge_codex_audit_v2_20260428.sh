#!/usr/bin/env bash
# v2: Use merge commit's changed-files (squash-merged) + check gate status, not just blocking_findings.
set -euo pipefail
cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
export VNX_DATA_DIR=$PWD/.vnx-data
export VNX_STATE_DIR=$PWD/.vnx-data/state
export VNX_CODEX_HEADLESS_MODEL=gpt-5.4

REPORT=.vnx-data/unified_reports/20260428-postmerge-codex-audit-v2.md
mkdir -p $(dirname "$REPORT")
{
  echo "# Post-merge Codex Audit v2 — 2026-04-28"
  echo ""
  echo "**v2 fixes**: uses merge commit's diff (not empty main-vs-main); checks status field; honors required_reruns."
  echo "**Started**: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""
  echo "## Per-PR results"
  echo ""
} > "$REPORT"

# pr → (merge_commit, original_branch)
get_merge_commit() {
  case "$1" in
    279) echo "47476ee" ;;
    280) echo "7fea010" ;;
    281) echo "9cb0ac2" ;;
    282) echo "dc96dd8" ;;
    283) echo "bab58d5" ;;
    284) echo "8ad14a5" ;;
    285) echo "8c81d32" ;;
  esac
}

ALL_CLEAN=true

for pr in 279 280 281 282 283 284 285; do
  merge=$(get_merge_commit "$pr")
  echo "### PR #$pr (merge $merge)" >> "$REPORT"

  # Derive changed-files from merge commit's diff against parent
  CHANGED=$(git diff --name-only "${merge}^..${merge}" | tr '\n' ',' | sed 's/,$//')
  echo "  - Changed files: $(echo "$CHANGED" | tr ',' '\n' | wc -l | tr -d ' ') files" >> "$REPORT"

  rm -f .vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json

  # Run with --changed-files explicitly + capture exit code (no || true)
  set +e
  python3 scripts/review_gate_manager.py request-and-execute \
    --pr $pr --branch main \
    --changed-files "$CHANGED" \
    --review-stack codex_gate --risk-class low --mode final \
    --dispatch-id 20260428-pr${pr}-postmerge-audit-v2 --json > /tmp/audit-v2-pr${pr}.json 2>&1
  RC=$?
  set -e

  RESULT=.vnx-data/state/review_gates/results/pr-${pr}-codex_gate.json
  if [ -f "$RESULT" ]; then
    STATUS=$(python3 -c "import json; d=json.load(open('$RESULT')); print(d.get('status',''))")
    BL=$(python3 -c "import json; d=json.load(open('$RESULT')); print(len(d.get('blocking_findings',[])))")
    AD=$(python3 -c "import json; d=json.load(open('$RESULT')); print(len(d.get('advisory_findings',[])))")
    DUR=$(python3 -c "import json; d=json.load(open('$RESULT')); print(f\"{d.get('duration_seconds',0):.1f}s\")")
    REQUIRED_RERUN=$(python3 -c "import json; d=json.load(open('$RESULT')); print(len(d.get('required_reruns',[])))")
    echo "  - Status: $STATUS (rc=$RC) blocking=$BL advisory=$AD required_reruns=$REQUIRED_RERUN duration=$DUR" >> "$REPORT"

    # Real success: status=completed AND blocking=0 AND no required_reruns
    if [ "$STATUS" = "completed" ] && [ "$BL" -eq 0 ] && [ "$REQUIRED_RERUN" -eq 0 ]; then
      echo "  - ✅ codex clean" >> "$REPORT"
    else
      ALL_CLEAN=false
      if [ "$BL" -gt 0 ]; then
        python3 -c "
import json
d = json.load(open('$RESULT'))
for f in d.get('blocking_findings',[]):
    print('  - BLOCKING: ' + f.get('message','')[:300].replace(chr(10),' '))
" >> "$REPORT"
        python3 scripts/open_items_manager.py add --dispatch 20260428-pr${pr}-postmerge-audit-v2 --severity warn --title "Codex post-merge finding on PR #$pr (v2 audit)" --pr $pr --details "Found by post-merge codex audit v2 at $(date -u). Audit used merge commit diff. See $RESULT for details." --report "$REPORT" 2>&1 | tail -1 >> "$REPORT" || true
      fi
      if [ "$STATUS" != "completed" ]; then
        echo "  - ⚠️ gate did NOT complete cleanly (status=$STATUS rc=$RC)" >> "$REPORT"
      fi
    fi
  else
    echo "  - ❌ no result file produced" >> "$REPORT"
    ALL_CLEAN=false
  fi
  echo "" >> "$REPORT"
done

echo "## Summary" >> "$REPORT"
if [ "$ALL_CLEAN" = "true" ]; then
  echo "All 7 PRs codex-clean ✅" >> "$REPORT"
  python3 scripts/open_items_manager.py close OI-1181 --reason "Post-merge codex audit v2 clean — all 7 PRs (#279-#285) pass codex gate. Audit report: $REPORT" 2>&1 | tail -1 >> "$REPORT" || true
else
  echo "Issues detected — see per-PR section + new OIs filed (1190+ range)" >> "$REPORT"
fi
echo "**Completed**: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$REPORT"
echo "AUDIT v2 COMPLETE — see $REPORT"
