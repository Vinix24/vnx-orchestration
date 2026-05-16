# Cost-Routed LLM Dispatch System — Architecture Design

**Target:** 1,000,000 dispatches/day across 10 projects, routed across Claude (premium), DeepSeek (mid), Kimi (budget). Self-hosted, restart-safe, ±2% daily cost accuracy, human approval gate above configurable threshold.

---

## 0. Load Anchors

| Metric | Value |
|---|---|
| Mean throughput | 11.6 dispatches/sec (1M / 86,400 s) |
| Diurnal peak (3× mean) | 34.7 dispatches/sec |
| Burst ceiling (10 min, 5× mean) | 57.9 dispatches/sec |
| Assumed avg token shape | 4,000 input + 1,500 output = 5,500 tok/dispatch |
| Daily token volume | 5.5 B tokens |
| Per-project mean | 100,000 dispatches/day (5× skew: heaviest project ~350K/day) |

All capacity decisions below are sized to the burst ceiling (58 rps), not the mean.

---

## 1. Architecture

### 1.1 Component Overview

```
Clients / Projects
        │ HTTP POST /v1/dispatches
        ▼
┌─────────────────────────────────────────────────────┐
│  Ingress API  (3 replicas, FastAPI, stateless)       │
│  • Auth (API key → project_id)                       │
│  • Schema validation + token pre-estimation          │
│  • Idempotency-key check (Postgres UNIQUE)           │
│  • Writes dispatch row → Postgres (sync commit)      │
│  • Enqueues dispatch_id → Redis Stream               │
└────────┬────────────────────────────────────────────┘
         │ XADD dispatch:inbox
         ▼
┌─────────────────────────────────────────────────────┐
│  Redis 7 (single primary + async replica)            │
│  Stream: dispatch:inbox                              │
│  Stream: dispatch:approval                           │
│  KV: provider:health:{claude|deepseek|kimi}          │
│  KV: budget:day:{project_id}                        │
└──────┬──────────────┬──────────────┬───────────────┘
       │ XREADGROUP    │              │
       ▼               ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
│  Router      │ │  Approval    │ │  Health Monitor  │
│  (2 replicas)│ │  Gate (1)    │ │  (1, 10s tick)  │
│  • classify  │ │  • webhook   │ │  • probes        │
│  • score     │ │  • approve   │ │  • circuit break │
│  • assign    │ │  • reject    │ │  • updates Redis │
└──────┬───────┘ └──────────────┘ └──────────────────┘
       │ XADD dispatch:routed:{provider}
       ▼
┌─────────────────────────────────────────────────────┐
│  Provider Worker Pools                               │
│  ┌──────────────┐ ┌─────────────┐ ┌─────────────┐  │
│  │ Claude       │ │ DeepSeek    │ │ Kimi         │  │
│  │ 6 replicas   │ │ 12 replicas │ │ 6 replicas   │  │
│  │ 4 conc each  │ │ 8 conc each │ │ 6 conc each  │  │
│  │ = 24 slots   │ │ = 96 slots  │ │ = 36 slots   │  │
│  └──────┬───────┘ └──────┬──────┘ └──────┬───────┘  │
└─────────┼────────────────┼───────────────┼──────────┘
          │ HTTPS          │ HTTPS         │ HTTPS
          ▼                ▼               ▼
     api.anthropic   api.deepseek    api.moonshot
          │
          ▼ (on completion)
┌─────────────────────────────────────────────────────┐
│  Postgres 15 (primary + 1 sync replica)             │
│  Tables: dispatch, cost_ledger_event, cost_rollup   │
│  WAL archived to local MinIO every 60s              │
└──────────────────────────────────────────────────────┘
          │
          ▼ (cost_ledger_event rows)
┌─────────────────────────────────────────────────────┐
│  Cost Aggregator (1 replica, 10s tick)              │
│  • Reads cost_ledger_event watermark                │
│  • Upserts cost_rollup_10s                          │
│  • Updates budget:day:{project_id} in Redis         │
└──────────────────────────────────────────────────────┘
```

### 1.2 Component Responsibilities

| Component | Stateless? | Scale unit | Sizing at 58 rps burst | Communication |
|---|---|---|---|---|
| **Ingress API** | yes | 1 replica = 150 rps capacity | 3 replicas (450 rps headroom, 7.8× burst) | sync HTTP in; async Redis XADD out |
| **Postgres primary** | no | vertical; 1 sync replica | 8 vCPU / 32 GB RAM / NVMe; PgBouncer transaction-mode 150 conns | direct pool, fsync=on, synchronous_commit=on |
| **Redis 7** | no | single primary (1 replica for reads) | 4 vCPU / 16 GB; stream backlog ≤500 MB at peak; appendonly yes | XADD/XREADGROUP; AOF every second |
| **Router** | yes | 1 replica = 800 dispatches/sec (scoring in <1 ms) | 2 replicas (1,600/sec, 27× burst) | XREADGROUP from `dispatch:inbox`; XADD to `dispatch:routed:{provider}` |
| **Approval Gate** | yes | 1 replica | 1 replica (approval rate << 1% of dispatches) | XREADGROUP from `dispatch:approval`; webhook out; HTTP in |
| **Health Monitor** | yes | 1 replica | 1 replica; 10s probe each provider | writes to Redis KV |
| **Claude workers** | yes | 1 pod = 4 concurrent slots | 6 pods = 24 slots | XREADGROUP `dispatch:routed:claude`; HTTPS to Anthropic |
| **DeepSeek workers** | yes | 1 pod = 8 concurrent slots | 12 pods = 96 slots | XREADGROUP `dispatch:routed:deepseek`; HTTPS to DeepSeek |
| **Kimi workers** | yes | 1 pod = 6 concurrent slots | 6 pods = 36 slots | XREADGROUP `dispatch:routed:kimi` ; HTTPS to Moonshot |
| **Cost Aggregator** | yes | 1 replica | 1 replica; processes 1M events/day at 11.6/sec | reads Postgres; writes Postgres + Redis KV |

### 1.3 Communication Design Rationale

**Sync only at the edge.** The ingress API returns 202 synchronously after Postgres commit. Everything downstream is async via Redis Streams.

**Redis Streams, not NATS or RabbitMQ.** Redis Streams provide durable consumer groups with message acknowledgment, per-message TTL, and dead-letter via pending entry list (PEL). A single Redis instance handles 200K+ XADD/sec — the 58 rps burst is trivially within capacity. Operational surface is smaller than NATS JetStream for this throughput class.

**Postgres as state authority, Redis as queue.** Redis holds only dispatch IDs in transit; all state lives in Postgres. This avoids split-brain between queue contents and dispatch state. Redis is rebuildable from Postgres (any un-ACKed dispatch with `state IN ('RECEIVED','ROUTED')` and `published_at IS NOT NULL` can be re-enqueued from the outbox).

**Per-provider streams.** `dispatch:routed:claude`, `dispatch:routed:deepseek`, `dispatch:routed:kimi` are separate streams. A Claude 429 storm does not stall DeepSeek consumers.

---

## 2. Data Flow

### 2.1 Dispatch State Machine

```
RECEIVED → ROUTING → ROUTED → [APPROVAL_PENDING] → QUEUED → IN_FLIGHT
                                                              │
                                              ┌───────────────┤
                                              │               │
                                         COMPLETED      FAILED_RETRYABLE
                                                              │
                                                    (≤5 attempts) → QUEUED
                                                    (>5 attempts) → FAILED_TERMINAL
```

State transitions use optimistic CAS: `UPDATE dispatch SET state=$new WHERE dispatch_id=$id AND state=$expected`. Zero rows updated = concurrent write won; caller re-reads.

### 2.2 Step-by-Step Write Log

| Step | Actor | Write | Durability guarantee |
|---|---|---|---|
| 1. POST /v1/dispatches | Ingress | `INSERT dispatch(..., state='RECEIVED')` + `INSERT dispatch_outbox(dispatch_id)` in one txn; `synchronous_commit=on` to sync replica | Survives primary crash; sync replica has the row |
| 2. Enqueue | Ingress (post-txn) | `XADD dispatch:inbox * dispatch_id {id}` + `UPDATE dispatch_outbox SET published_at=now()` | Redis AOF fsync=everysec; worst-case 1s of messages rebuildable from outbox |
| 3. Return 202 | Ingress | Response body: `{dispatch_id, status:"received"}` | Client may retry with same `Idempotency-Key`; second POST returns existing dispatch_id |
| 4. Classify | Router | `UPDATE dispatch SET state='ROUTING'` CAS; run scoring (in-memory); `UPDATE dispatch SET state='ROUTED', provider=$p, est_cost_usd=$c, routed_at=now()` | Double-classify blocked by CAS on RECEIVED→ROUTING |
| 5a. Gate (cost ≥ threshold) | Router | `UPDATE dispatch SET state='APPROVAL_PENDING'`; `XADD dispatch:approval * dispatch_id {id}` | Approval Gate consumes; webhook fired; message persists until explicit ACK |
| 5b. Gate (below threshold) | Router | `UPDATE dispatch SET state='QUEUED'`; `XADD dispatch:routed:{provider} * dispatch_id {id}` | Provider worker consumes |
| 6. Approve | Approval Gate | `UPDATE dispatch SET state='QUEUED', approved_by=$user, approved_at=now()`; `XADD dispatch:routed:{provider}` | Approval row committed before publish |
| 7. Worker claim | Provider worker | Redis `XREADGROUP` (message enters PEL for group); `UPDATE dispatch SET state='IN_FLIGHT', worker_id=$w, lease_expires_at=now()+4min` | PEL holds message until XACK; lease prevents duplicate execution |
| 8. Provider call | Worker | HTTPS to provider; response buffered in memory | n/a |
| 9. Completion write | Worker | Txn: `UPDATE dispatch SET state='COMPLETED', input_tokens=$i, output_tokens=$o, total_cost_usd=$c, completed_at=now()` + `INSERT cost_ledger_event(...)` | Single Postgres txn; both rows committed or neither |
| 10. ACK | Worker | `XACK dispatch:routed:{provider} {msg_id}` | Only after Postgres txn commits; crash before ACK = PEL redelivery; worker idempotent (checks existing COMPLETED state) |
| 11. Aggregate | Cost Aggregator | Every 10s: `INSERT INTO cost_rollup_10s SELECT ... FROM cost_ledger_event WHERE event_id > $watermark`; advances watermark | Watermark in `aggregator_state` table; restart-safe |

### 2.3 Restart Durability (single-node constraint)

| Failure point | What's at risk | Recovery |
|---|---|---|
| Ingress dies before Postgres commit | Nothing persisted | Client retries; idempotency key prevents double-insert |
| Ingress dies after commit, before Redis XADD | Row in dispatch (RECEIVED) + outbox row with `published_at=NULL` | Outbox poller (30s tick) re-enqueues all `published_at IS NULL` rows |
| Router dies mid-classify | Dispatch stuck in RECEIVED or ROUTING | Redis PEL redelivers `dispatch:inbox` message after `ack_wait=60s`; CAS on ROUTING prevents double-classify |
| Worker dies mid-call | Dispatch stuck in IN_FLIGHT; PEL holds message | Lease sweeper (every 30s) detects `lease_expires_at < now()` AND `state='IN_FLIGHT'`; resets state to QUEUED; worker re-enqueues to provider stream; attempt_n++ |
| Postgres primary dies | 15s write gap during Patroni failover | Sync replica promoted RPO=0; PgBouncer reconnects via VIP; in-flight work either committed on replica or replayed from client retry |
| Redis primary dies | Messages in streams since last AOF flush (≤1s) | Outbox poller rebuilds queue from dispatch table rows with `state IN ('RECEIVED','ROUTED','QUEUED')` and `published_at IS NOT NULL` but no XACK |

---

## 3. Cost Routing Logic

### 3.1 Provider Pricing

| Provider | Input $/MTok | Output $/MTok | Concurrency limit (assumed) |
|---|---|---|---|
| Claude | $3.00 | $15.00 | 4,000 RPM / project |
| DeepSeek | $0.14 | $0.87 | 60,000 RPM |
| Kimi | $0.60 | $4.00 | 10,000 RPM |

Cost estimate per dispatch: `(input_tok * input_rate + output_tok * output_rate) / 1_000_000`

### 3.2 Routing Algorithm (deterministic, applied in sequence)

**Stage 1: Capability filter.** Remove providers that cannot satisfy hard requirements.

| Requirement | Providers excluded |
|---|---|
| Vision / image input | DeepSeek, Kimi |
| Context > 128K tokens | DeepSeek (128K max) |
| Strict tool_use with parallel calls | Kimi |
| SLA: latency_slo_ms = 500 (interactive) | Kimi (p95 ~8s for 1500 output) |

**Stage 2: Health filter.** Remove providers where `provider:health:{name}` = `degraded` or `down` (set by Health Monitor; TTL 30s; re-set every 10s by probe).

**Stage 3: Budget check.** For each surviving provider, check:
```
budget:day:{project_id}:spend + est_cost_usd(provider) <= budget:day:{project_id}:envelope
```
Remove providers that would exceed the project's daily budget envelope. If all providers are excluded → `FAILED_TERMINAL(budget_exhausted)`.

**Stage 4: Cost-optimal assignment.** Score each survivor:

```python
score = est_cost_usd * WEIGHT_COST + latency_p95_ms * WEIGHT_LATENCY

WEIGHT_COST    = 1.0 / max_daily_budget_usd  # normalizes to [0,1]
WEIGHT_LATENCY = 0.0001 / latency_slo_ms     # secondary; dominates only when cost is identical
```

Assign the provider with the lowest score.

**Stage 5: Task-type override.** Certain task types override Stage 4 with a fixed primary assignment:

| task_type | Primary | Fallback chain |
|---|---|---|
| `code_generation` | DeepSeek | Kimi → Claude |
| `code_review` | Claude | DeepSeek |
| `refactoring` | DeepSeek | Claude |
| `documentation` | Kimi | DeepSeek → Claude |
| `debugging` | Claude | DeepSeek |
| `design` | Claude | Kimi → DeepSeek |
| `translation` | DeepSeek | Kimi |
| `classification` | Kimi | DeepSeek |
| `summarization` | Kimi | DeepSeek |

Override is applied unless the primary is excluded by Stage 1-3. If the primary is excluded, walk the fallback chain in order.

**Stage 6: Approval gate.** If `est_cost_usd > project.approval_threshold_usd` (default $1.00; per-project config in Postgres `project_config` table), dispatch enters `APPROVAL_PENDING`. Operator receives webhook payload (Slack or email) with a signed one-click approve URL (HMAC-SHA256, 4h TTL). Rejected dispatches → `FAILED_TERMINAL(approval_rejected)`.

### 3.3 Runtime Fallback (provider failure during execution)

```
attempt 1: assigned provider
attempt 2: next in fallback chain (Stage 5 table)
attempt 3: cheapest provider passing Stage 1-2 (price-blind, ignores Stage 4-5)
attempt 4-5: same as attempt 3, with exponential backoff (30s, 90s)
attempt 6: FAILED_TERMINAL; ops alert
```

Each attempt writes to `dispatch_attempts(dispatch_id, attempt_n, provider, error_code, started_at, ended_at, input_tokens_consumed)`. Input tokens consumed by failed attempts are recorded in `cost_ledger_event` with `attempt_status='failed'`; they count toward the daily cost ledger.

### 3.4 Expected Cost Mix (modeled)

Assumed task distribution: 25% code_gen, 15% code_review, 15% refactor, 10% docs, 10% debug, 5% design, 10% translation, 10% classify/summarize.

| Provider | Dispatch share | Token volume/day | Daily provider cost |
|---|---|---|---|
| Claude | 20% (200K dispatches) | 1.1 B tok | $2,420 |
| DeepSeek | 55% (550K dispatches) | 3.0 B tok | $1,053 |
| Kimi | 25% (250K dispatches) | 1.4 B tok | $1,400 |
| **Total** | 100% | **5.5 B tok** | **$4,873/day** |

All-Claude baseline at 5.5 B tok/day: ~$32,250/day. Cost routing saves ~$27,400/day (85% reduction).

---

## 4. Failure Modes

### 4.1 Provider Rate Limit Storm (Claude 429s sustained >30s)

**Detection:** Worker tracks `429_count` per 60s window in a local counter; at 429_rate > 10%, emits a `provider_throttled` event. Health Monitor independently runs a 10s synthetic probe to each provider; on 3 consecutive failures it writes `provider:health:claude = degraded` to Redis (TTL 30s). Prometheus alert `provider_error_rate_1m{provider="claude"} > 0.10`.

**Recovery:** Router Stage 2 filters out `claude` immediately for new dispatches. Existing Claude workers drain their in-flight queue but stop pulling new messages. Dispatches requiring Claude-only capabilities (vision, interactive SLO) enter a `throttle_hold` queue (separate Redis stream `dispatch:throttle_hold:claude`); a reconciler re-routes them every 30s once health is restored. Dispatches that can fall back proceed immediately to DeepSeek/Kimi.

**Blast radius:** Claude-only dispatches (~5% of volume) delayed up to 5 min. All other dispatches unaffected. No data loss.

### 4.2 Postgres Primary Failure (15s outage)

**Detection:** PgBouncer health check fails (connect timeout 3s × 3 attempts = 9s); Patroni leader election fires; `pg_up{role="primary"} == 0` Prometheus alert.

**Recovery:** Patroni promotes sync replica (RPO=0, RTO ≈ 15s). PgBouncer reconnects via floating VIP (updated by Patroni's `on_role_change` callback). Ingress returns 503 during the window; clients retry with idempotency keys. Outbox poller and lease sweeper resume automatically on reconnect; no in-flight dispatch is lost because: (a) all commits were synchronously replicated, and (b) Redis PEL holds un-ACKed messages.

**Blast radius:** 15s write unavailability; ingress drops ~870 requests (58 rps × 15s). All retried successfully with idempotency keys. Zero data loss.

### 4.3 Worker Pod Crash Mid-Execution (Tokens Already Consumed)

**Detection:** Dispatch state `IN_FLIGHT` with `lease_expires_at < now()`. Lease sweeper runs every 30s; detects the orphaned row.

**Recovery:** Sweeper executes: `UPDATE dispatch SET state='QUEUED', attempt_n=attempt_n+1 WHERE state='IN_FLIGHT' AND lease_expires_at < now()`. Re-adds dispatch_id to the appropriate provider stream. Replacement worker picks it up. Before calling the provider, the worker checks `dispatch_attempts` for a prior `request_hash` match; if the provider supports idempotency keys (Anthropic does), reuses the same key to avoid a duplicate charge. For DeepSeek and Kimi (no idempotency key support), a second provider call is made and both token events are recorded in `cost_ledger_event`.

**Blast radius:** At 4 concurrent slots per Claude pod, a crash affects ≤4 dispatches. Maximum daily duplicate-call count at 0.1% failure rate = 1,000 dispatches (±$5 cost impact). Exceeding 0.5% duplicate rate triggers a ops alert.

### 4.4 Cost Ledger Drift (>2% gap vs. Provider Invoice)

**Detection:** Daily reconciler job at 03:00 UTC fetches yesterday's usage from provider APIs (Anthropic `/usage`, DeepSeek dashboard export, Moonshot billing API). Compares against `SELECT SUM(cost_usd) FROM cost_ledger_event WHERE DATE(recorded_at) = $yesterday GROUP BY provider`. If `ABS(api_invoice - ledger_sum) / api_invoice > 0.02`, alert fires.

**Recovery:** Reconciler writes a `cost_ledger_correction` event with the delta amount; `cost_rollup_daily` is recalculated for the affected day. Root causes captured by the reconciler: (a) failed-attempt input tokens not recorded (fix: always insert `cost_ledger_event` even on provider error), (b) worker crash after provider call but before Postgres commit (the 4-min lease window is the max exposure: 58 rps × 240s × 5,500 tok × $0.0000087/tok ≈ $6.60 maximum un-committed cost). Structural fix: reduce max exposure by writing cost row before issuing provider call with `input_tokens_est` and reconciling actual tokens on completion.

**Blast radius:** Budget enforcement off by ≤2% for ≤24h; corrected in the next daily cycle. No dispatch loss.

### 4.5 Redis Primary Failure

**Detection:** Sentinel (3-node) detects primary unavailability after `down-after-milliseconds 5000`. Ingress, Router, and Workers lose XADD/XREADGROUP ability. Prometheus `redis_up == 0`.

**Recovery:** Sentinel promotes the async replica within 10s (RPO ≤ 1s of AOF data). During the gap, Ingress falls back to Postgres-only mode: dispatch row is written + outbox row, but Redis XADD is skipped. Outbox poller detects `published_at IS NULL` rows and re-enqueues them after Redis recovers. Workers drain their in-process buffers; no new messages consumed during outage.

**Blast radius:** 10s of message queue unavailability. Dispatches submitted during gap are queued in Postgres outbox; re-enqueued within 30s of Redis recovery. Worst case: 580 dispatches delayed by ≤40s. No data loss.

### 4.6 Budget Envelope Exhaustion (Project Overspend)

**Detection:** Cost Aggregator updates `budget:day:{project_id}:spend` in Redis every 10s. Router checks this before each routing decision. At 95% of envelope, Router emits `budget.warning` event (webhook to project owner). At 100%, new dispatches for the project receive `FAILED_TERMINAL(budget_exhausted)`.

**Recovery:** Ops console allows budget envelope increase (requires human action; writes to `project_config.budget_envelope_usd`). Auto-increase is not implemented in v1; human gate is intentional.

**Blast radius:** Per-project; other projects unaffected. Project receives warning at 95%, has ~5% of budget window (~$50-500 depending on envelope) to act before dispatches start failing.

### 4.7 Approval Queue Backlog (Human Gate Bottleneck)

**Detection:** Prometheus metric `dispatch_approval_pending_count > 50` for >30 min. Approval Gate exposes `/metrics` with current queue depth.

**Recovery:** Auto-escalation after `project.auto_approve_after_minutes` (default 30 min) for dispatches with `est_cost_usd < project.auto_approve_ceiling_usd` (default $2.00). Dispatches above the ceiling remain pending and trigger a second webhook to a fallback approver. Unresolved after 4h: `FAILED_TERMINAL(approval_timeout)`.

**Blast radius:** Per-project; other projects unaffected. Cost-above-ceiling dispatches delayed up to 4h or rejected.

---

## 5. SLOs

### 5.1 Latency Targets (end-to-end: POST /v1/dispatches → 200 response with result body)

| Tier | Provider | Typical output | p50 | p95 | p99 | Notes |
|---|---|---|---|---|---|---|
| **Interactive** | Claude | ≤1,500 tok | 1.5 s | 4.0 s | 8.0 s | Our overhead budget: +300 ms on top of provider raw p95 |
| **Standard** | DeepSeek | ≤1,500 tok | 2.0 s | 6.0 s | 14 s | Dominant path (55% of volume); provider p95 ~5.5s |
| **Batch** | Kimi | ≤3,000 tok | 4.5 s | 16 s | 35 s | Async polling acceptable; result webhook in v1.1 |

Internal latency budget (POST received → Redis XADD complete): p99 ≤ 40 ms. Exceeding this signals Ingress CPU or Postgres contention.

Routing latency (XADD → provider call issued): p99 ≤ 200 ms. Exceeding this signals Router consumer lag.

### 5.2 Throughput Targets

| Metric | Value |
|---|---|
| Sustained throughput floor | 35 dispatches/sec for 1h without SLO breach |
| Burst ceiling (tested) | 58 dispatches/sec for 10 min |
| Capacity headroom alert | Trigger autoscale when 15-min avg > 25 dispatches/sec (70% of floor) |
| Scale unit: Claude workers | +1 pod (4 slots) when `dispatch:routed:claude` PEL depth > 80 |
| Scale unit: DeepSeek workers | +1 pod (8 slots) when PEL depth > 160 |
| Scale unit: Kimi workers | +1 pod (6 slots) when PEL depth > 120 |

### 5.3 Error Budget (30-day rolling window)

| SLI | Target (availability) | Monthly error budget |
|---|---|---|
| Ingress availability (2xx+202 / total excluding 4xx) | 99.90% | 43.2 min |
| Dispatch completion within tier p99 latency | 99.00% | 1% of dispatches ≈ 10,000/day |
| Cost ledger accuracy vs. provider invoice | ≥98% (±2%) | Reconciliation gap ≤24h to close |
| Approval gate response within business hours | p95 ≤ 15 min | Breach = escalation page |
| Provider call error rate (5xx + timeout, excluding 429) | ≤0.5% | Breaching 0.5% triggers ops alert |

### 5.4 SLO Violation Detection

**Metrics:** Prometheus with the following key series:
- `dispatch_e2e_latency_seconds{tier, provider}` — histogram; burn-rate alerts on p95/p99.
- `dispatch_state_total{state, project_id}` — counter; APPROVAL_PENDING queue depth derived.
- `dispatch_error_total{error_type, provider}` — counter.
- `cost_ledger_drift_ratio{provider}` — gauge; set by daily reconciler.
- `provider_health_status{provider}` — gauge (0=ok, 1=degraded, 2=down).
- `redis_stream_pending_count{stream}` — gauge; consumer lag.

**Burn-rate alerts (Google SRE multi-window):**
- Page: `(error_rate_1h > 14.4 × budget_rate) AND (error_rate_5m > 14.4 × budget_rate)` — 2% budget consumed in 1h.
- Ticket: `(error_rate_6h > 6 × budget_rate) AND (error_rate_30m > 6 × budget_rate)` — 5% budget consumed in 6h.

**Synthetic probes:** 1 dispatch per provider per minute via a dedicated `probe_project_id` that does not count against real project budgets. Probes use a fixed 50-token ping prompt with a deterministic expected output hash. Probe SLO is independent of production SLO.

**Dashboards:** Grafana boards:
1. System overview — throughput, provider split, error rate, budget burn rate.
2. Per-project view — dispatches/hour, spend/hour, approval queue depth, per-provider distribution.
3. Provider health — p50/p95/p99 per provider, 429 rate, probe success rate.
4. Cost — daily cost by provider vs. budget envelope, ledger drift gauge, top-10 expensive dispatches.

---

## 6. Schema (Canonical Tables, Abridged)

```sql
CREATE TABLE dispatch (
  dispatch_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id         TEXT NOT NULL REFERENCES project_config(project_id),
  idempotency_key    TEXT NOT NULL,
  state              TEXT NOT NULL CHECK (state IN (
                       'RECEIVED','ROUTING','ROUTED','APPROVAL_PENDING',
                       'QUEUED','IN_FLIGHT','COMPLETED',
                       'FAILED_RETRYABLE','FAILED_TERMINAL')),
  task_type          TEXT NOT NULL,
  provider           TEXT,
  worker_id          TEXT,
  attempt_n          SMALLINT NOT NULL DEFAULT 0,
  latency_slo_ms     INT NOT NULL DEFAULT 2000,
  est_input_tokens   INT,
  est_output_tokens  INT,
  est_cost_usd       NUMERIC(10,6),
  input_tokens       INT,
  output_tokens      INT,
  total_cost_usd     NUMERIC(10,6),
  approval_required  BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by        TEXT,
  approved_at        TIMESTAMPTZ,
  lease_expires_at   TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  routed_at          TIMESTAMPTZ,
  queued_at          TIMESTAMPTZ,
  in_flight_at       TIMESTAMPTZ,
  completed_at       TIMESTAMPTZ,
  state_changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_code         TEXT,
  UNIQUE (project_id, idempotency_key)
);

CREATE INDEX idx_dispatch_state        ON dispatch (state) WHERE state NOT IN ('COMPLETED','FAILED_TERMINAL');
CREATE INDEX idx_dispatch_lease        ON dispatch (lease_expires_at) WHERE state = 'IN_FLIGHT';
CREATE INDEX idx_dispatch_project_day  ON dispatch (project_id, (created_at::date));
CREATE INDEX idx_dispatch_outbox       ON dispatch_outbox (published_at) WHERE published_at IS NULL;

CREATE TABLE dispatch_outbox (
  dispatch_id   UUID PRIMARY KEY REFERENCES dispatch,
  published_at  TIMESTAMPTZ
);

CREATE TABLE dispatch_attempts (
  id              BIGSERIAL PRIMARY KEY,
  dispatch_id     UUID NOT NULL REFERENCES dispatch,
  attempt_n       SMALLINT NOT NULL,
  provider        TEXT NOT NULL,
  error_code      TEXT,
  input_tokens    INT,
  started_at      TIMESTAMPTZ NOT NULL,
  ended_at        TIMESTAMPTZ,
  UNIQUE (dispatch_id, attempt_n)
);

CREATE TABLE cost_ledger_event (       -- append-only; never UPDATE
  event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dispatch_id     UUID NOT NULL,
  attempt_n       SMALLINT NOT NULL,
  attempt_status  TEXT NOT NULL CHECK (attempt_status IN ('success','failed')),
  provider        TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  input_tokens    INT NOT NULL,
  output_tokens   INT NOT NULL,
  cost_usd        NUMERIC(10,6) NOT NULL,
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (dispatch_id, attempt_n, provider)
);
CREATE INDEX idx_cost_ledger_provider_day ON cost_ledger_event (provider, (recorded_at::date));
CREATE INDEX idx_cost_ledger_project_day  ON cost_ledger_event (project_id, (recorded_at::date));

CREATE TABLE cost_rollup_10s (
  bucket_ts      TIMESTAMPTZ NOT NULL,
  provider       TEXT NOT NULL,
  project_id     TEXT NOT NULL,
  cost_usd       NUMERIC(12,6) NOT NULL DEFAULT 0,
  dispatch_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket_ts, provider, project_id)
);

CREATE TABLE project_config (
  project_id              TEXT PRIMARY KEY,
  budget_envelope_usd     NUMERIC(10,2) NOT NULL,
  approval_threshold_usd  NUMERIC(10,4) NOT NULL DEFAULT 1.00,
  auto_approve_ceiling_usd NUMERIC(10,4) NOT NULL DEFAULT 2.00,
  auto_approve_after_min  INT NOT NULL DEFAULT 30,
  approval_webhook_url    TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE aggregator_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Stores: last_processed_event_id for cost aggregator watermark
```

---

## 7. Capacity and Infrastructure Cost

| Component | Spec | Estimated monthly cost (bare-metal/VPS) |
|---|---|---|
| Postgres HA (2 nodes + Patroni) | 8 vCPU / 32 GB / 1 TB NVMe each | $600 |
| Redis (primary + replica + 3 Sentinel) | 4 vCPU / 16 GB / 200 GB SSD each × 3 | $300 |
| Worker pods — Claude (6) | 2 vCPU / 4 GB each | $200 |
| Worker pods — DeepSeek (12) | 2 vCPU / 4 GB each | $400 |
| Worker pods — Kimi (6) | 2 vCPU / 4 GB each | $200 |
| Ingress API (3) + Router (2) | 2 vCPU / 4 GB each | $200 |
| MinIO (WAL archive, 4 TB) | 2 vCPU / 8 GB / 4 TB | $150 |
| Observability (Prometheus + Grafana + Alertmanager) | 4 vCPU / 16 GB / 2 TB | $250 |
| **Infra subtotal** | | **$2,300/mo** |
| **Provider cost (modeled, 5.5 B tok/day × 30 days)** | | **$146,200/mo** |
| **Total** | | **$148,500/mo for 30M dispatches = $0.0050/dispatch** |

All-Claude baseline at 5.5 B tok/day × 30 days: ~$967,500/mo. Cost routing saves **$819,000/mo** at this scale.

---

## 8. What v1 Does Not Include

- Multi-region active-active (single region with HA; DR via WAL restore from MinIO).
- Streaming results to caller (sync response only; client polls `GET /v1/dispatches/{id}` for async-tier results until webhook added in v1.1).
- Dynamic prompt compression (could reduce input tokens 10-15% — v1.2).
- Fine-tuned fallback model hosted on local GPU (v2.0 if provider costs grow).
- Per-dispatch spend alerts to project owners in real time (batched webhooks only in v1).
