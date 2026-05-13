# Burn-in Monitoring Runbook — Wave 1 + Wave 5

**Date**: 2026-05-12
**Scope**: Mission Control + Sales Copilot pilot deployments
**Purpose**: Concrete commands to verify that the post-rc1 work (Wave 1 shadow read cutover + Wave 5 smart-context injection) is actually firing on dispatched work, and to measure the +30pp dispatch-quality lift in production.

---

## What "burn-in" means here

Two parallel things need validation on real dispatches before the v1.0.0 final tag is cut:

| Wave | What it does | Burn-in question |
|---|---|---|
| **Wave 1** (shadow read cutover) | Reads central state via the new path in parallel with the legacy path; logs divergences. | Does the new path return the same answers as the legacy path? Any divergences = central-reader regression. |
| **Wave 5** (smart context injection) | Injects prior-round findings, ADR matches, code anchors, operator memory, schema introspection into worker context at dispatch time. | Does the +30pp dispatch-quality lift (measured on 658 historical dispatches) hold up on new dispatches? |

Both depend on dispatches actually going through `subprocess_dispatch.py`. Direct `claude --print` invocations bypass both — see `T0/CLAUDE.md` §Dispatch-routing in mc + sales-copilot.

---

## Pre-flight: confirm the dispatch path

Before measuring anything, verify each dispatch is going through the canonical path. Run in the target project:

```bash
# 1. Recent T1 events stream — should have entries from today
ls -la .vnx-data/events/T1.ndjson
wc -l .vnx-data/events/T1.ndjson

# 2. Recent receipts in t0_receipts.ndjson — should have entries from today
tail -5 .vnx-data/state/t0_receipts.ndjson

# 3. Active dispatch manifest — confirm dispatch was routed via subprocess_dispatch.py
ls .vnx-data/dispatches/active/
# Each active dir should have manifest.json (from subprocess_dispatch entry)

# 4. Lease state — should show terminal busy DURING active work
sqlite3 .vnx-data/state/runtime_coordination.db \
  "SELECT terminal_id, state, dispatch_id FROM terminal_leases;"
```

If `T1.ndjson` is empty after running real dispatches, T0 used direct `claude --print` (bypassing VNX governance). Update T0's `.claude/terminals/T0/CLAUDE.md` per `T0/CLAUDE.md` §Dispatch-routing, restart T0.

---

## Wave 1 — Shadow read divergence

Wave 1 added a `shadow_logger` (NDJSON, lazy-written) that records both legacy-read and central-read results for the same query. Divergences = bugs in the central reader.

### Read the shadow log

```bash
# Path is project-local
SHADOW_LOG=.vnx-data/state/shadow_divergence.ndjson  # adjust if differs

# Quick count
wc -l "$SHADOW_LOG" 2>/dev/null || echo "no shadow log yet"

# Recent divergences (last hour)
tail -100 "$SHADOW_LOG" 2>/dev/null | python3 -c "
import json, sys, time
from datetime import datetime, timedelta
cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
count = 0
diverged = 0
for line in sys.stdin:
    try:
        e = json.loads(line)
        if e.get('timestamp', '') < cutoff:
            continue
        count += 1
        if e.get('diverged'):
            diverged += 1
            print(f\"DIVERGE: {e.get('read_site')} key={e.get('key')} legacy={str(e.get('legacy_value'))[:40]} central={str(e.get('central_value'))[:40]}\")
    except: pass
print(f'\\nTotal reads (last 1h): {count}; diverged: {diverged}')
"
```

### Decision rule

| Diverged in last 24h | Verdict |
|---|---|
| 0 divergences over ≥100 reads | Wave 1 cutover **READY** for hot-path migration |
| 1-5 divergences | Investigate each manually; could be expected (timing windows, legacy bugs we're fixing) |
| >5 divergences or any blocking-class | Wave 1 **NOT READY** — file OI per divergence pattern |

Per the strategic replan v1.2: Wave 1 success criterion is "shadow read cutover validates clean for 3 weeks before reader cutover."

---

## Wave 5 — Smart context injection (+30pp lift)

Wave 5 measures dispatch quality success-rate. Baseline: 88.9% with naïve injection. Target: mid-90s after P0-P4.

### Per-dispatch injection evidence

**Recorded injection facts** — the authoritative source is `intelligence_injections` in the
quality DB (written by `IntelligenceSelector.record_injection`):

```bash
sqlite3 .vnx-data/state/quality_intelligence.db <<EOF
.mode column
.headers on
SELECT dispatch_id, injection_type, role, created_at
FROM intelligence_injections
WHERE created_at > datetime('now', '-1 day')
ORDER BY created_at DESC
LIMIT 20;
EOF
```

**Live event stream** — the archived events NDJSON is a secondary check for streamed
event records (only present for subprocess-routed terminals; empty live file = look in archive):

```bash
# Pick a recent dispatch_id
DISPATCH_ID=$(ls -t .vnx-data/dispatches/completed/ | head -1)

# Read its manifest
cat ".vnx-data/dispatches/completed/$DISPATCH_ID/manifest.json" | python3 -m json.tool

# Find the dispatch's archived events
EVENTS=".vnx-data/events/archive/T1/$DISPATCH_ID.ndjson"
# (or T2/T3 — check whichever terminal handled it)

# Filter for injection events in the live event stream
grep -i "intelligence_injection\|smart_context\|adr_match\|prior_round\|code_anchor\|operator_memory\|schema_introspect" "$EVENTS" 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line)
        t = e.get('type', '?')
        d = e.get('data', {})
        print(f\"  {t}: {d.get('name','')} {str(d.get('payload',''))[:80]}\")
    except: pass
"
```

### Quality success-rate from quality DB

```bash
# Quality DB lives in .vnx-data/state/quality_intelligence.db
sqlite3 .vnx-data/state/quality_intelligence.db <<'EOF'
.headers on
.mode column
SELECT
  date(timestamp) as day,
  COUNT(*) as dispatches,
  SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) as successes,
  ROUND(100.0 * SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) / COUNT(*), 1) as pct
FROM dispatch_outcomes
WHERE timestamp > date('now', '-14 days')
GROUP BY day
ORDER BY day DESC;
EOF
```

### Per-injection-type effectiveness

```bash
sqlite3 .vnx-data/state/quality_intelligence.db <<'EOF'
.headers on
.mode column
SELECT
  injection_type,
  COUNT(*) as fired,
  SUM(CASE WHEN dispatch_outcome='success' THEN 1 ELSE 0 END) as successes,
  ROUND(100.0 * SUM(CASE WHEN dispatch_outcome='success' THEN 1 ELSE 0 END) / COUNT(*), 1) as pct
FROM intelligence_injections
WHERE created_at > date('now', '-14 days')
GROUP BY injection_type
ORDER BY fired DESC;
EOF
```

### Per-provider segmentation

After Wave 4.5 PR-3 merges, codex and gemini worker dispatches also emit
`intelligence_injection` events (prior-round findings, ADR matches, code anchors,
operator memory, schema intro) in addition to standard antipattern/pattern items.

Split the quality success-rate query by `model_provider` to compare Claude vs
non-Claude lift:

```sql
-- Per-provider success rate (last 14 days)
SELECT
  model_provider,
  date(timestamp) AS day,
  COUNT(*) AS dispatches,
  SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS successes,
  ROUND(
    100.0 * SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) / COUNT(*), 1
  ) AS pct
FROM dispatch_outcomes
WHERE timestamp > date('now', '-14 days')
GROUP BY model_provider, day
ORDER BY model_provider, day DESC;
```

Use this query to confirm that codex/gemini-routed dispatches show the same
intelligence injection event (look for `intelligence_injection` in the archived
events) and that their success rate trend is comparable to the Claude baseline.

**Note**: Intelligence injection for codex/gemini dispatches only fires when the
dispatch includes `role` or `dispatch_metadata`.  Gate-runner invocations (PR-4.5.2b,
currently deferred as #473) are a separate scope and will be tracked under a future
runbook update.

### Decision rule

| 7-day success rate | Verdict |
|---|---|
| ≥93% | Wave 5 **on target** — push toward v1.0.0 final |
| 88-93% | Hold; investigate which injection-type isn't lifting |
| <88% | Regression vs 88.9% baseline — Wave 5 kill-switch criteria per replan §Wave 5 |
| Token-bloat alerts in injection logs | Wave 5 P5 cleanup needed before final tag |

---

## Cost monitoring

Both Wave 5 injections cost tokens (extra context = extra input tokens). Per-dispatch cost:

```bash
# Aggregate today's costs by terminal
sqlite3 .vnx-data/state/quality_intelligence.db <<'EOF'
.headers on
.mode column
SELECT
  terminal_id,
  COUNT(*) as dispatches,
  ROUND(SUM(input_tokens) / 1000.0, 1) as input_k,
  ROUND(SUM(output_tokens) / 1000.0, 1) as output_k,
  ROUND(SUM(cost_usd), 2) as cost_usd
FROM dispatch_costs
WHERE timestamp > date('now', '-1 day')
GROUP BY terminal_id;
EOF
```

If injection-token-bloat pushes per-dispatch input above ~30k tokens, file an OI for Wave 5 P5 (token-budget enforcement).

---

## Health checks

### Are daemons up?

```bash
# Dispatcher daemon (must run for dispatches to be picked up)
pgrep -fl "dispatcher_v8_minimal\|dispatcher_supervisor" | head -3

# Receipt processor
pgrep -fl "receipt_processor" | head -3

# Smart tap (legacy pickup, optional with subprocess_dispatch)
pgrep -fl "smart_tap" | head -3
```

If dispatcher is missing in mc/sales-copilot:

```bash
# Start from project root:
.claude/vnx-system/scripts/dispatcher_supervisor.sh &
.claude/vnx-system/scripts/receipt_processor_supervisor.sh &
```

### Lease state sanity

```bash
sqlite3 .vnx-data/state/runtime_coordination.db <<'EOF'
SELECT terminal_id, state, dispatch_id,
       CAST((julianday('now') - julianday(lease_acquired_at)) * 86400 AS INTEGER) AS age_seconds
FROM terminal_leases;
EOF
```

A `state='busy'` lease older than 60 minutes without an active claude subprocess = stale; release via:

```bash
python3 .claude/vnx-system/scripts/runtime_core_cli.py release-on-failure \
  --terminal T1 --dispatch-id <stuck-id> --generation <gen-from-db> \
  --reason "stale_lease_manual_cleanup"
```

---

## When to stop burn-in and cut v1.0.0 final

Per strategic replan v1.2 §Wave 1/5 success criteria:

- [ ] 3 weeks Wave 1 shadow with zero blocking divergences
- [ ] 7 consecutive days Wave 5 success rate ≥93% (per injection-effective analysis)
- [ ] Zero unresolved P0 OIs (currently: OI-1369 + OI-1370 — fix PRs in flight today)
- [ ] codex_gate infra restored (currently broken — codex CLI binary missing)
- [ ] PR #462 (CFX-W5-2) + #463 (queue-popup default) + #464 (T0 routing) merged

When all green: tag `v1.0.0` from main (drops the `-rc1` suffix), cut GitHub Release, mark as Latest (replaces v0.5.0's badge).
