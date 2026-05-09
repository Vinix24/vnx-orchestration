# Wave 1 Rollback Procedures

**Date:** 2026-05-09
**Authority:** claudedocs/2026-05-09-wave1-design.md §8
**Applies to:** Wave 1 shadow-mode central DB cutover (PRs #450–#454)

---

## Overview

Wave 1 wires shadow-mode reads at 4 VNX read sites. The 3-state flag
`VNX_USE_CENTRAL_DB` controls read routing:

| Flag value | Behaviour |
|---|---|
| unset (default) | Per-project DB only — no change from pre-Wave-1 |
| `shadow` | Both DBs; per-project authoritative; divergences logged |
| `1` | Central DB only (post-pilot cutover) |

There are two rollback types: **soft** (per-project flag unset) and
**hard** (revert PRs in reverse order). The correct type depends on
the trigger condition.

---

## Trigger Conditions

### Immediate hard rollback triggers

These require stopping the pilot immediately and reverting PRs:

- **Metric 1 violation** — any row returned with `project_id` ≠ requested project
  → cross-tenant contamination; data integrity at risk
- **Metric 5 violation** — same lease key held by two different projects simultaneously
  → state corruption; hard rollback + investigate before restarting

### Soft rollback triggers (investigate first)

These warrant disabling shadow mode for the affected project and investigating:

- **Metric 2 violation** — PR-scoped blocking finding missing in central
  → governance-critical; soft rollback for affected project until root cause found
- **Metric 3 violation** — top-3 IntelligenceSelector output differs
  → injection quality degraded; soft rollback for affected project
- **Metric 4 violation (count drift)** — row counts differ between DBs
  → structural mismatch; soft rollback + investigate migration
- **Metric 4 violation (checksum drift > 0.01%)** — investigate first;
  if repeated on consecutive days, soft rollback
- **Metric 6 violation** — central reads consistently > 1.5× slower
  → performance concern; add index to central DB before re-enabling

---

## Soft Rollback — Per-Project Flag Unset

**When:** metric 2, 3, 4, or 6 violation for a specific project.

**Effect:** reverts that project to per-project DB reads. No data loss.
Shadow mode stays active for other projects.

### Step 1: Identify affected project

```bash
# Check which project is logging divergences
tail -f .vnx-data/state/shadow_divergence.ndjson | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    print(e['project_id'], e['metric_id'], e['severity'], e['read_site'])
"
```

### Step 2: Unset flag for affected project

```bash
# For the affected terminal/session running that project:
unset VNX_USE_CENTRAL_DB

# If set in .env or per-project config, remove the line:
grep -r VNX_USE_CENTRAL_DB ~/.vnx-data/<project_id>/ .vnx-data/ configs/
```

### Step 3: Verify rollback took effect

```bash
# Run shadow_report.py — should show no new divergences for that project
python3 scripts/shadow_report.py --since=1h --project=<project_id>

# Confirm per-project DB is being read (should see no shadow log entries)
python3 scripts/runtime_core_cli.py check-state --project=<project_id>
```

### Step 4: Verify reads back on per-project DB

```bash
# Spot-check: query per-project DB directly and compare with dashboard output
sqlite3 .vnx-data/state/quality_intelligence.db \
  "SELECT COUNT(*) FROM success_patterns"

# Dashboard should show the same count
curl -s http://localhost:8765/api/operator/system-health | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['components']['intelligence_db']['details'])"
```

---

## Hard Rollback — Full Wave 1 Revert

**When:** metric 1 or metric 5 violation, OR soft rollback insufficient.

**Effect:** reverts all 5 Wave 1 PRs. Per-project DBs remain authoritative
throughout Wave 1, so no data is lost — worst case is loss of central reads.

### Step 1: Stop all shadow-mode reads immediately

```bash
# Unset flag for ALL active projects
unset VNX_USE_CENTRAL_DB

# Verify no shadow-mode sessions are running:
ps aux | grep "VNX_USE_CENTRAL_DB=shadow"
```

### Step 2: Identify which PRs to revert

Wave 1 PRs in merge order (revert in **reverse** order):

| PR | Commit | Description |
|---|---|---|
| PR-W1.5 | TBD | Dashboard shadow wiring + canary tests + rollback docs |
| PR-W1.4 | `1c1a52b` | IntelligenceSelector + DispatchRegister shadow wiring |
| PR-W1.3 | `4c742f4` | T0 state-builder shadow wiring |
| PR-W1.2 | `8996a21` | shadow_logger NDJSON writer |
| PR-W1.1 | `ec3b9a4` | shadow_verifier comparator |

### Step 3: Revert PR-W1.5 first

```bash
# Find the merge commit for PR-W1.5
gh pr view <PR_W1.5_NUMBER> --json mergeCommit --jq .mergeCommit.oid

# Revert (creates a new revert commit — do NOT force-push)
git revert <merge_commit_sha> --no-commit
git commit -m "revert(wave1): revert PR-W1.5 — dashboard shadow wiring

Rollback triggered by metric violation. Per-project DB remains authoritative.
See docs/operations/wave1-rollback.md for trigger conditions."
```

### Step 4: Revert remaining PRs in reverse order

```bash
# Repeat for PR-W1.4, W1.3, W1.2, W1.1 if needed
git revert <merge_commit_pr_w1_4> --no-commit
git commit -m "revert(wave1): revert PR-W1.4 — IntelligenceSelector shadow wiring"

git revert <merge_commit_pr_w1_3> --no-commit
git commit -m "revert(wave1): revert PR-W1.3 — T0 state-builder shadow wiring"

# Continue for W1.2 and W1.1 if full rollback required
```

**Note:** You do NOT need to revert all 5 PRs if only the dashboard wiring
(W1.5) is causing the issue. Revert only what is needed, then investigate
before re-applying.

### Step 5: Push and create revert PR

```bash
git push origin HEAD

# Create a revert PR (do NOT merge to main without CI green)
gh pr create \
  --title "revert(wave1): rollback Wave 1 shadow wiring" \
  --body "Triggered by metric violation. See divergence log for root cause.
  
  **Trigger:** <describe the violation>
  **Affected project:** <project_id>
  **Rollback scope:** PR-W1.5 (and W1.4/W1.3/W1.2/W1.1 if cascaded)"
```

### Step 6: Verify rollback complete

```bash
# All Wave 1 read-site files should no longer contain VNX_USE_CENTRAL_DB
grep -rn "VNX_USE_CENTRAL_DB" scripts/build_t0_state.py \
  scripts/lib/intelligence_selector.py \
  scripts/lib/dispatch_register.py \
  dashboard/api_intelligence.py \
  dashboard/api_operator.py

# Expected output after full rollback: no matches

# Run full test suite to confirm no regressions
pytest tests/ -x -q
```

---

## Verification Commands After Rollback

Use these commands to confirm the system is reading from per-project DBs:

```bash
# 1. No shadow divergences in last hour
python3 scripts/shadow_report.py --since=1h
# Expected: "No divergences found" or empty output

# 2. Per-project DB row counts match dashboard
sqlite3 .vnx-data/state/quality_intelligence.db \
  "SELECT COUNT(*) FROM success_patterns; SELECT COUNT(*) FROM antipatterns;"

# 3. Shadow mode not active
echo "VNX_USE_CENTRAL_DB=${VNX_USE_CENTRAL_DB:-<unset>}"
# Expected: <unset>

# 4. System health endpoint shows per-project DB counts
curl -s http://localhost:8765/api/operator/system-health | python3 -m json.tool | grep -A5 intelligence_db

# 5. T0 state reads are stable (no shadow errors in logs)
python3 scripts/build_t0_state.py --format brief 2>&1 | head -20
```

---

## Re-enabling After Investigation

Once root cause is resolved (data migration re-run, index added, bug fixed):

```bash
# Confirm canary tests still pass
pytest tests/canary/ -v

# Re-enable shadow for lowest-risk project first
export VNX_USE_CENTRAL_DB=shadow

# Monitor for 24h before re-enabling on other projects
python3 scripts/shadow_report.py --since=24h --follow
```

**Do NOT flip to `VNX_USE_CENTRAL_DB=1` (production cutover) until**:
- All 6 metrics show 0 violations for ≥7 consecutive days
- Operator has reviewed and signed off on the divergence dashboard
- CI is green on main with Wave 1 PRs in place

---

## Contact and Escalation

If rollback procedure is unclear or metric violations are ambiguous:

1. Check `docs/operations/wave1-rollback.md` (this file) §Trigger Conditions
2. Run `python3 scripts/shadow_report.py --since=7d` for full divergence history
3. Review Wave 1 design: `claudedocs/2026-05-09-wave1-design.md` §8
4. Escalate to operator — T0 may not autonomously decide hard rollback for metric 1 or 5
