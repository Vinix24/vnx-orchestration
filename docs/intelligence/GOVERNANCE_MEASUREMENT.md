# Governance Measurement System

**Version**: 1.1.0
**Added**: 2026-03-07
**Updated**: 2026-03-07
**Owner**: T-MANAGER
**Schema**: 8.2.0-cqs-advisory-oi

## Overview

The Governance Measurement System replaces self-reported status with objective, calculated quality scores. It addresses three fundamental measurement problems that undermined the reliability of VNX quality data:

| Problem | Impact | Solution |
|---------|--------|----------|
| Self-reported status | 166 unique status values, no standardization | Normalized to 5 categories + weighted CQS |
| Success != done | `task_complete` = terminal says done, not verified | Composite score from multiple objective signals |
| Timeout != failure | 30% receipt timeouts pollute quality metrics | Timeouts excluded from quality calculations |

### Industry Context

The system applies **Statistical Process Control (SPC)** — the same methodology used in manufacturing quality (Toyota Production System, Six Sigma) and modern software delivery (DORA metrics, Accelerate). SPC provides:

- **Control charts** with statistically-derived limits (X-bar +/- 3 sigma) that distinguish normal variation from anomalies
- **Western Electric rules** for early detection of trends, shifts, and runs before they become critical
- **First-Pass Yield (FPY)** — the percentage of work completed correctly on the first attempt, the single most important quality metric in both manufacturing and software delivery

These are not arbitrary thresholds. A 3-sigma control limit means a point outside it has only a 0.27% probability of occurring by chance — making false alarms rare while catching real problems early.

## Architecture: 3-Layer Governance

```
                    VNX Intelligence Feedback Loop
                    ==============================

 DISPATCH                    EXECUTION                    RECEIPT
 --------                    ---------                    -------
   T0 dispatches task         Terminal executes            Receipt arrives
   with intelligence          with patterns +              with status +
   context + patterns         prevention rules             report path
        |                          |                           |
        v                          v                           v
 +--------------+          +---------------+          +----------------+
 | Intelligence |          |  Session Log  |          | Receipt        |
 | Injection    |          |  (JSONL)      |          | Enrichment     |
 | - patterns   |          |  - tokens     |          | - quality      |
 | - prevention |          |  - errors     |          |   advisory     |
 | - tags       |          |  - tool calls |          | - git          |
 | - model hint |          |  - model      |          |   provenance   |
 +--------------+          +---------------+          +----------------+
        |                          |                           |
        |                          |                           |
        +------------+-------------+---------------------------+
                     |
                     v
        +---------------------------+
        |   Layer 1: CQS Calculator |  <-- Real-time, per dispatch
        |   (receipt-time scoring)   |
        |                           |
        |   Inputs:                 |
        |   - normalized_status     |
        |   - completion signals    |
        |   - token efficiency      |
        |   - error density         |
        |   - rework detection      |
        |                           |
        |   Output: CQS 0-100      |
        +---------------------------+
                     |
                     | stored in dispatch_metadata
                     v
        +---------------------------+
        |  Layer 2: Nightly         |  <-- Aggregated, statistical
        |  Governance Aggregator    |
        |                           |
        |  Computes per scope:      |
        |  - First-Pass Yield       |
        |  - Rework Rate            |
        |  - Gate Velocity          |
        |  - Mean CQS              |
        |                           |
        |  SPC Control Charts:      |
        |  - X-bar +/- 3 sigma      |
        |  - Western Electric rules |
        |  - Anomaly alerts         |
        +---------------------------+
                     |
                     | governance_metrics + spc_alerts
                     v
        +---------------------------+
        |  Layer 3: Weekly Report   |  <-- Comparative, actionable
        |                           |
        |  - System FPY trend       |
        |  - Model comparison       |
        |    (controlled for role)  |
        |  - Role effectiveness     |
        |  - Gate bottlenecks       |
        |  - Top 5 actions          |
        +---------------------------+
                     |
                     | feeds back into
                     v
        +---------------------------+
        |  Intelligence Loop        |
        |  - Pattern confidence     |
        |    adjustment             |
        |  - Model routing hints    |
        |  - Suggested edits        |
        |  - Prevention rules       |
        +---------------------------+
                     |
                     | next dispatch cycle
                     v
                  DISPATCH
                  (improved)
```

## Intelligence Feedback Loop: Complete Data Flow

The governance system closes the loop between dispatch quality measurement and future dispatch improvement. Here is the complete closed-loop architecture showing how conversational logs, receipts, and statistical analysis feed back into the intelligence system:

```
+=====================================================================+
|                    CLOSED-LOOP INTELLIGENCE SYSTEM                   |
+=====================================================================+

  1. DISPATCH ENRICHMENT (real-time)
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  gather_intelligence.py
       |
       +-- Agent validation (skills.yaml registry)
       +-- Pattern matching (FTS5, top 5 by relevance)
       +-- Prevention rules (tag-based, 1-4 per task)
       +-- Model routing hints (from t0_session_brief.json)   <----+
       +-- Quality context injection                                |
       |                                                            |
       v                                                            |
  2. EXECUTION + LOGGING                                            |
  ~~~~~~~~~~~~~~~~~~~~~~~~~~                                        |
  Claude Code terminal session                                      |
       |                                                            |
       +-- JSONL session log (tokens, tools, errors, model)         |
       +-- Report generation (markdown)                             |
       +-- Git provenance (commits, branch, diff)                   |
       |                                                            |
       v                                                            |
  3. RECEIPT PROCESSING (real-time)                                  |
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~                                  |
  receipt_processor_v4.sh + append_receipt.py                        |
       |                                                            |
       +-- Quality advisory (code checks on changed files)          |
       +-- Terminal snapshot                                        |
       +-- Dispatch metadata update (outcome_status)                |
       +-- CQS calculation + persistence  <-- NEW                   |
       |        |                                                   |
       |        +-- Status normalization (166 -> 5 categories)      |
       |        +-- Completion signal scoring                       |
       |        +-- Effort efficiency (vs role median)              |
       |        +-- Error density analysis                          |
       |        +-- Rework detection (same gate+pr_id)              |
       |                                                            |
       v                                                            |
  4. NIGHTLY ANALYSIS PIPELINE                                      |
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~                                   |
  conversation_analyzer_nightly.sh                                   |
       |                                                            |
       +-- Phase 1: Session parsing                                 |
       |   conversation_analyzer.py                                 |
       |   - Parse JSONL -> session_analytics DB                    |
       |   - Extract model, tokens, tools, errors                   |
       |   - Deep analysis (heuristic + LLM)                       |
       |                                                            |
       +-- Phase 1.5: Session-dispatch linkage                      |
       |   link_sessions_dispatches.py                              |
       |   - Cross-reference sessions with dispatches               |
       |   - Enable per-dispatch token analysis                     |
       |                                                            |
       +-- Phase 2: T0 session brief                                |
       |   generate_t0_session_brief.py                             |
       |   - Model performance aggregation (7-day window)           |
       |   - Model routing hints generation  ---------------------->+
       |   - Active concerns detection                              |
       |                                                            |
       +-- Phase 2.5: Governance aggregation  <-- NEW               |
       |   governance_aggregator.py                                 |
       |   - CQS backfill for unscored dispatches                  |
       |   - FPY, rework rate, gate velocity per scope              |
       |   - SPC control limit computation                          |
       |   - Anomaly detection (Western Electric rules)             |
       |                                                            |
       +-- Phase 3: Suggested edits                                 |
       |   generate_suggested_edits.py                              |
       |   - Pattern-based system tuning suggestions -------------->+
       |   - Human-in-the-loop review workflow                      |
       |                                                            |
       +-- Phase 4: Email digest (opt-in)                           |
       |   - Model performance + routing hints                      |
       |   - Pending suggested edits                                |
       |   - SPC alerts summary                                     |
       |                                                            |
       v                                                            |
  5. LEARNING LOOP (daily 18:00)                                    |
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~                                   |
  learning_loop.py + intelligence_daemon.py                          |
       |                                                            |
       +-- Pattern confidence adjustment                            |
       |   - Used patterns: +10% (cap 2.0)                         |
       |   - Ignored patterns: -5% (floor 0.1)                     |
       |   - Success patterns: +15%                                 |
       |   - Failure patterns: -10%                                 |
       |                                                            |
       +-- Pattern archival (unused 30+ days)                       |
       +-- Prevention rule generation (from tag combinations)       |
       +-- Cache invalidation and preloading                        |
       |                                                            |
       +-------> Feeds back to step 1 (next dispatch) ------------>+

  6. WEEKLY GOVERNANCE REPORT (Sunday 03:00 or manual)
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  governance_weekly_report.py
       |
       +-- System FPY trend (4 weeks)
       +-- Model comparison (controlled for role/complexity)
       +-- Role effectiveness ranking
       +-- Gate bottleneck identification
       +-- Top 5 actionable items from SPC data
       |
       v
  docs/governance/week_YYYY_WW.md
```

### Key Insight: Why Self-Reported Status is Insufficient

The VNX system previously relied on terminals reporting their own success. Analysis revealed:

- **166 unique status strings** across all dispatches — no standardization
- **100% Sonnet success rate** according to self-reported data — statistically implausible
- **30% of dispatches** receive `no_confirmation` (receipt timeout), which is a system issue, not a task failure
- **Rework is invisible**: When a "successful" task requires re-dispatch to the same gate, the original dispatch still shows as successful

The CQS replaces this with objective measurement from multiple independent signals.

## Layer 1: Composite Quality Score (CQS)

### Calculation

CQS is a weighted 0-100 score computed per dispatch from 7 independent signals:

| Component | Weight | Source | Calculation |
|-----------|--------|--------|-------------|
| Status | 25% | receipt.status | 166 raw statuses mapped to 5 categories |
| Completion | 20% | receipt | Has report? PR merged? Gate passed? |
| Effort | 15% | session_analytics | Token usage vs median for same role |
| Error density | 10% | session JSONL | Error/fail message ratio |
| Rework | 10% | dispatch_metadata | Same gate+pr_id dispatched before? |
| T0 Advisory | 10% | quality_advisory | T0 decision (approve/followup/hold) blended with risk_score |
| OI Delta | 10% | open_items.json | Open items created vs resolved balance |

### Status Normalization

Raw status values are mapped to 5 normalized categories:

| Category | Score | Example Raw Values |
|----------|-------|--------------------|
| `success` | 100 | task_complete, success, merged, done, approved, gate_passed |
| `partial` | 60 | partial, needs_review, in_progress, pending_review |
| `failure` | 0 | task_failed, error, rejected, failed, blocked |
| `timeout` | excluded | no_confirmation, timeout, receipt_timeout |
| `unknown` | excluded | anything not in the mapping |

**Critical design decision**: `timeout` and `unknown` statuses are excluded from all quality metrics. This prevents system-level issues (receipt processing delays) from contaminating quality data. Only dispatches with a definitive quality outcome (success/partial/failure) receive a CQS score.

### Effort Efficiency Scoring

Token usage is compared against the median for the same role:

| Ratio (actual/median) | Score | Interpretation |
|------------------------|-------|----------------|
| <= 0.5x | 100 | Highly efficient |
| 0.5x - 1.0x | 80-100 | Normal efficiency |
| 1.0x - 2.0x | 20-80 | Below average |
| > 2.0x | 0-20 | Significant overhead |

### Rework Detection

A dispatch is flagged as rework if another dispatch exists with the same `gate` and `pr_id` combination. First attempts score 100, rework attempts score 0. This makes rework visible in the aggregate metrics.

### T0 Advisory Scoring

The T0 quality advisory produces a decision (`approve`, `approve_with_followup`, `hold`) and a risk score (0-100). CQS blends these with 70/30 weighting:

| Decision | Decision Score |
|----------|---------------|
| `approve` | 100 |
| `approve_with_followup` | 60 |
| `hold` | 0 |

```
T0 Advisory Score = decision_score * 0.7 + (100 - risk_score) * 0.3
```

When no advisory data exists (historical dispatches), the component scores **50.0 (neutral)**.

### Open Items Delta Scoring

Measures the quality debt impact of a dispatch — were quality issues created or resolved?

| Signal | Effect |
|--------|--------|
| Each item resolved | +15 points (cap 30) |
| Each item created | -10 points (cap 30) |
| Targeted items unresolved | -20 per unresolved (cap 20) |
| No OI involvement | 50.0 (neutral) |

Dispatches can reference target open items via `OI-NNN` patterns in instructions. When a dispatch targets an OI but doesn't resolve it, the penalty makes this measurable.

### target_open_items Dispatch Enrichment

Open item references are captured at dispatch time through two mechanisms:

1. **Formal**: `--target-open-items` argument to `log_dispatch_metadata.py` (JSON array or comma-separated)
2. **Fallback**: Regex scan of dispatch instructions for `OI-\d{3,}` patterns in `dispatcher_v8_minimal.sh`

Resolution is tracked via `closed_by_dispatch_id` on open items when they are closed.

### Example CQS Calculation

```
Dispatch: gate_pr4_excel_quality, role=backend-developer, model=claude-opus

Status:       task_complete -> success -> 100 * 0.25 = 25.0
Completion:   report=yes, pr=no, gate=yes -> 66.7 * 0.20 = 13.3
Effort:       48K tokens vs 52K median -> ratio 0.92 -> 84 * 0.15 = 12.6
Error density: 2 errors / 45 messages -> 4.4% -> 80 * 0.10 = 8.0
Rework:       first attempt -> 100 * 0.10 = 10.0
T0 Advisory:  approve, risk_score=10 -> 97 * 0.10 = 9.7
OI Delta:     0 created, 1 resolved -> 65 * 0.10 = 6.5
                                                    --------
CQS = 85.1
```

### Database Schema

Six columns on `dispatch_metadata` (3 original CQS + 3 OI delta):

```sql
ALTER TABLE dispatch_metadata ADD COLUMN cqs REAL;
ALTER TABLE dispatch_metadata ADD COLUMN normalized_status TEXT;
ALTER TABLE dispatch_metadata ADD COLUMN cqs_components TEXT;  -- JSON breakdown
ALTER TABLE dispatch_metadata ADD COLUMN target_open_items TEXT;       -- JSON array ["OI-042"]
ALTER TABLE dispatch_metadata ADD COLUMN open_items_created INTEGER DEFAULT 0;
ALTER TABLE dispatch_metadata ADD COLUMN open_items_resolved INTEGER DEFAULT 0;
```

### Integration Points

CQS is computed at two points for reliability:

1. **Receipt enrichment** (`append_receipt.py`): Computed during quality advisory enrichment and persisted immediately
2. **Receipt processing** (`receipt_processor_v4.sh` section C3b): Standalone update via `update_dispatch_cqs.py` as fallback

## Layer 2: Nightly Governance Aggregation

### Metrics

Six metrics are computed per scope (system/terminal/role/gate/model):

| Metric | Formula | Industry Standard |
|--------|---------|-------------------|
| **First-Pass Yield (FPY)** | Unique tasks succeeded first try / total unique tasks | Toyota Production System, Six Sigma |
| **Rework Rate** | Total dispatches / unique (gate, pr_id) combos | Manufacturing quality, DORA change failure rate |
| **Gate Velocity** | Hours from first dispatch to successful gate completion | DORA lead time for changes |
| **Mean CQS** | AVG(cqs) WHERE cqs IS NOT NULL | Composite quality index |
| **Dispatch Count** | Total dispatches in period | Volume/throughput metric |
| **OI Resolution Rate** | SUM(resolved) / (SUM(created) + SUM(resolved)) | Quality debt velocity |

#### First-Pass Yield (FPY)

FPY is the gold standard quality metric in lean manufacturing and increasingly in software delivery. It measures the percentage of work items that pass through a process correctly the first time, without requiring rework, correction, or re-dispatch.

**Why FPY matters more than success rate**: A gate with 90% success rate sounds good, but if it takes 3 attempts to reach success, the true FPY is only 33%. FPY exposes the hidden cost of rework.

**Chain Reliability**: For multi-gate features, overall reliability is the product of individual FPY rates:

```
Feature FPY = FPY_gate1 * FPY_gate2 * FPY_gate3
Example:     0.80      * 0.75      * 0.85       = 0.51 (51%)
```

This explains why features with many gates often have long delivery times — even individually high FPY rates compound to poor overall throughput.

### SPC Control Charts

Statistical Process Control uses control limits derived from the data itself (not arbitrary thresholds):

```
UCL = X-bar + 3 * sigma    (Upper Control Limit)
CL  = X-bar                (Center Line / Mean)
LCL = X-bar - 3 * sigma    (Lower Control Limit, floor at 0)
```

These are recomputed nightly from a 30-day rolling baseline. A point outside the control limits has a 0.27% probability of occurring by chance — making it a statistically significant signal.

### Anomaly Detection (Western Electric Rules)

Four rules from the Western Electric Handbook (1956), still the standard in SPC:

| Rule | Condition | Severity | Signal |
|------|-----------|----------|--------|
| **Out of control** | Point beyond UCL or LCL | Critical | Process has shifted |
| **Trend** | 7+ consecutive points increasing or decreasing | Warning | Drift in progress |
| **Shift** | 8+ consecutive points on same side of center line | Warning | Mean has shifted |
| **Run** | 2 of 3 points beyond 2-sigma | Info | Increased variability |

These rules detect problems early — a trend of 7 points is caught before it reaches the control limits.

### Database Schema

```sql
-- Aggregated metrics per scope and period
CREATE TABLE governance_metrics (
    period_start DATE, period_end DATE,
    scope_type TEXT,    -- 'system'|'terminal'|'role'|'gate'|'model'
    scope_value TEXT,
    metric_name TEXT,   -- 'fpy'|'rework_rate'|'gate_velocity_hours'|'mean_cqs'|'dispatch_count'
    metric_value REAL,
    sample_size INTEGER
);

-- SPC control limits (UPSERT on recalculation)
CREATE TABLE spc_control_limits (
    metric_name TEXT, scope_type TEXT, scope_value TEXT,
    center_line REAL, ucl REAL, lcl REAL, sigma REAL,
    sample_count INTEGER, baseline_start DATE, baseline_end DATE,
    UNIQUE(metric_name, scope_type, scope_value)
);

-- Anomaly alerts from Western Electric rules
CREATE TABLE spc_alerts (
    alert_type TEXT,    -- 'out_of_control'|'trend'|'shift'|'run'
    metric_name TEXT, scope_type TEXT, scope_value TEXT,
    observed_value REAL, control_limit REAL,
    description TEXT, severity TEXT
);
```

## Layer 3: Weekly Governance Report

Generated weekly (manual or cron). Produces `docs/governance/week_YYYY_WW.md` with:

1. **System Health**: FPY trend (4 weeks), rework trend, active SPC alerts
2. **Model Comparison**: Mean CQS and FPY per model, controlled for role mix
3. **Role Effectiveness**: FPY and rework rate per role
4. **Gate Bottlenecks**: Slowest gates, highest rework gates
5. **Top Actions**: Data-driven improvement suggestions from SPC alerts and metric outliers

### Model Comparison: Controlling for Confounders

Raw model comparison (Opus vs Sonnet) is misleading when models handle different task types. The report only compares models on the same role mix, ensuring the comparison reflects model capability rather than task difficulty.

## Pipeline Integration

### Nightly Pipeline (conversation_analyzer_nightly.sh)

```
Phase 0:   DB schema migrations (quality_db_init.py)
Phase 1:   Session parsing (conversation_analyzer.py)
Phase 1.5: Session-dispatch linkage (link_sessions_dispatches.py)
Phase 2:   T0 session brief (generate_t0_session_brief.py)
Phase 2.5: Governance aggregation + SPC  <-- NEW
Phase 3:   Suggested edits (generate_suggested_edits.py)
Phase 4:   Email digest (send_digest_email.py)
```

Phase 2.5 includes automatic CQS backfill for any dispatches that were completed before the governance system was installed.

### Receipt Processing Pipeline

```
Receipt arrives
  -> A. Enrichment (append_receipt.py)
       -> Quality advisory
       -> CQS calculation + DB persist  <-- NEW
  -> B. Terminal state update
  -> C. Dispatch metadata update
       -> C3: outcome_status + completed_at
       -> C3b: CQS update (fallback)    <-- NEW
  -> D. PR evidence
  -> E. Progress state
  -> F. Send to T0
```

## Files

| File | Purpose |
|------|---------|
| `scripts/lib/cqs_calculator.py` | CQS calculation engine + status normalization |
| `scripts/update_dispatch_cqs.py` | Standalone CQS update CLI |
| `scripts/governance_aggregator.py` | Nightly FPY/rework/SPC computation |
| `scripts/governance_weekly_report.py` | Weekly markdown governance report |
| `schemas/quality_intelligence.sql` | Schema v8.2.0 with governance tables + OI delta columns |
| `scripts/open_items_manager.py` | Resolution tracking (closed_by_dispatch_id) + programmatic close |
| `scripts/quality_db_init.py` | Migration for CQS columns + governance tables |
| `scripts/append_receipt.py` | CQS integration at receipt enrichment |
| `scripts/receipt_processor_v4.sh` | CQS fallback update in section C3b |
| `scripts/conversation_analyzer_nightly.sh` | Phase 2.5 governance pipeline |

## Operations

```bash
# Verify governance tables exist
python3 scripts/quality_db_init.py

# Backfill CQS for all existing dispatches (dry-run)
python3 scripts/governance_aggregator.py --dry-run --backfill

# Run governance aggregation (production)
python3 scripts/governance_aggregator.py --backfill

# Generate weekly report
python3 scripts/governance_weekly_report.py

# Query CQS distribution
sqlite3 $VNX_STATE_DIR/quality_intelligence.db \
  "SELECT normalized_status, COUNT(*), ROUND(AVG(cqs),1) FROM dispatch_metadata WHERE normalized_status IS NOT NULL GROUP BY normalized_status"

# Check SPC alerts
sqlite3 $VNX_STATE_DIR/quality_intelligence.db \
  "SELECT severity, alert_type, description FROM spc_alerts WHERE acknowledged_at IS NULL ORDER BY detected_at DESC LIMIT 10"

# View governance metrics for a scope
sqlite3 $VNX_STATE_DIR/quality_intelligence.db \
  "SELECT metric_name, metric_value, sample_size FROM governance_metrics WHERE scope_type='system' AND scope_value='all' ORDER BY period_start DESC LIMIT 20"
```

## References

- Wheeler, D.J. & Chambers, D.S. (1992). *Understanding Statistical Process Control*. SPC Press.
- Western Electric Company (1956). *Statistical Quality Control Handbook*. Western Electric.
- Forsgren, N., Humble, J., & Kim, G. (2018). *Accelerate: The Science of Lean Software and DevOps*. IT Revolution Press.
- Toyota Production System — Shingo, S. (1989). *A Study of the Toyota Production System*. Productivity Press.
- DORA Metrics — State of DevOps Reports (2018-2024). Google Cloud / DORA Team.
