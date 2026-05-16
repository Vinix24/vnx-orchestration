# Million-Dispatch Cost-Routed LLM System — Design

Target: **1,000,000 dispatches/day** across **10 projects** routed across Claude (premium), DeepSeek (mid), Kimi (budget). Self-hosted, single-node-restart-safe, ±2% daily cost accuracy, human approval gate.

## 0. Load Sizing (anchors for the rest of the doc)

| Metric | Value |
|---|---|
| Mean throughput | 11.6 dispatches/sec (1M / 86 400 s) |
| Diurnal peak (3× mean) | 35 dispatches/sec |
| Burst peak (10 min, 5× mean) | 58 dispatches/sec |
| Avg dispatch token shape | 5 000 input + 2 000 output = 7 000 tok |
| Daily token volume | 7.0 B tokens |
| Per-project mean | 100 000 dispatches/day (skew assumed 5×: top project = 350K/day) |

These four numbers (11.6, 35, 58, 7 000) drive every sizing decision below.

---

## 1. Architecture

### 1.1 Component map

```
                          ┌────────────────────────────────────────────┐
                          │              Operators / Projects          │
                          └──────────────────────┬─────────────────────┘
                                                 │ HTTPS POST /dispatches
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │  Ingress API (FastAPI, x4)  │   stateless
                                  │  – auth, schema validate    │   N = ceil(peak_rps × 25 ms / 1 cpu)
                                  │  – idempotency-key store    │
                                  └────────┬───────────┬────────┘
                                           │           │ INSERT
                                           │ PUBLISH   ▼
                                           │   ┌──────────────────────┐
                                           │   │  PostgreSQL 16 (HA)  │
                                           │   │  Patroni 3-node      │
                                           │   │  sync replica + WAL  │
                                           │   │  archive → MinIO     │
                                           │   └─────────▲────────────┘
                                           │             │ all state writes
                                           ▼             │
                                  ┌─────────────────────────────┐
                                  │  NATS JetStream cluster x3  │   subject: dispatch.routed.*
                                  │  R3 file-backed, fsync=on   │   subject: dispatch.completed
                                  └────────┬────────────────────┘
                                           │
                  ┌────────────────────────┼─────────────────────────┐
                  │                        │                         │
                  ▼                        ▼                         ▼
        ┌─────────────────┐     ┌─────────────────┐        ┌─────────────────┐
        │ Router/Classifier│     │  Approval Gate  │        │ Cost Aggregator │
        │  (x4, stateless)│     │  (x2, Postgres) │        │ (x2, 5 s tick)  │
        └────────┬────────┘     └────────┬────────┘        └─────────────────┘
                 │ PUBLISH                │ holds high-cost dispatches
                 ▼ provider-tagged
        ┌──────────────────────────────────────────────────────────────┐
        │   Provider Worker Pools (consumer groups per provider)       │
        │  ┌────────────┐   ┌────────────┐   ┌────────────┐            │
        │  │  Claude    │   │ DeepSeek   │   │  Kimi      │            │
        │  │  x8 pods   │   │  x16 pods  │   │  x8 pods   │            │
        │  │  conc=4    │   │  conc=8    │   │  conc=6    │            │
        │  └─────┬──────┘   └─────┬──────┘   └─────┬──────┘            │
        └────────┼─────────────────┼─────────────────┼──────────────────┘
                 │  HTTPS          │  HTTPS          │ HTTPS
                 ▼                 ▼                 ▼
            api.anthropic   api.deepseek      api.moonshot
```

### 1.2 Component responsibilities

| Component | Stateless? | Scaling unit | Sizing at peak (35 rps) | Comm pattern |
|---|---|---|---|---|
| **Ingress API** | yes | replicas | 4 replicas × 2 vCPU = 8 vCPU; sustains 400 rps headroom | sync HTTP → async enqueue |
| **PostgreSQL primary** | no | vertical + read replicas | 16 vCPU / 64 GB RAM / NVMe / sync replica | direct connection pool (PgBouncer transaction mode, 200 conns) |
| **NATS JetStream** | no | 3-node cluster | 8 vCPU / 16 GB / 500 GB NVMe per node | pub/sub with durable consumers |
| **Router/Classifier** | yes | replicas | 4 replicas × 2 vCPU; classify in <10 ms | consume `dispatch.created`, publish `dispatch.routed.{provider}` |
| **Approval Gate** | yes | replicas | 2 replicas × 1 vCPU | webhook-out, HTTP-in |
| **Provider workers** | yes | replicas × in-process concurrency | Claude 8×4=32, DeepSeek 16×8=128, Kimi 8×6=48 in-flight slots | pull from JetStream durable consumer, HTTPS to provider |
| **Cost Aggregator** | yes | replicas | 2 replicas; 5 s tumbling window | consume `dispatch.completed`, batch UPSERT into `cost_ledger_5s` |
| **MinIO (S3-compat)** | no | 4-node erasure-coded | 4 × 4 TB = 16 TB raw, ~10 TB usable | WAL archive, response payload archive |

### 1.3 Communication choices

- **Sync** only at the edge (Ingress, Approval Gate HTTP). Everything internal is async via JetStream.
- **Queue, not direct call**: workers pull from durable consumers; back-pressure naturally throttles ingress. A worker outage parks messages on the stream rather than dropping them.
- **Single source of truth = Postgres**. JetStream carries pointers (dispatch_id) + envelopes; payloads >32 KB are stored in Postgres `dispatch_payload`, JetStream message holds only the id.
- **Per-provider consumer groups** isolate rate-limit blast radius: a Claude 429 storm cannot stall DeepSeek workers.

### 1.4 Scaling unit definition (no hand-waving)

| Tier | Scale unit | Trigger (15-min avg) | Max units |
|---|---|---|---|
| Ingress API | 1 pod = 400 rps headroom | `ingress_p99_latency_ms > 200` | 12 |
| Router | 1 pod = 1 200 classifications/s | `router_lag_seconds > 5` | 12 |
| Claude worker | 1 pod = 4 concurrent in-flight | `claude_queue_depth > 100` AND `provider_429_rate < 1%` | 32 |
| DeepSeek worker | 1 pod = 8 concurrent | `deepseek_queue_depth > 200` | 64 |
| Kimi worker | 1 pod = 6 concurrent | `kimi_queue_depth > 150` | 32 |

The bottleneck at full peak (58 rps burst) is provider concurrency, **not** internal infra — internal headroom is ~10×.

---

## 2. Data Flow — Dispatch Lifecycle

### 2.1 States

```
RECEIVED → VALIDATED → ROUTED → [APPROVAL_PENDING] → IN_FLIGHT
                                                          │
                                                          ├─→ COMPLETED
                                                          ├─→ FAILED_RETRYABLE → IN_FLIGHT (≤3×)
                                                          └─→ FAILED_TERMINAL
```

State transitions are **always** a Postgres `UPDATE dispatch SET state=…, state_changed_at=now() WHERE dispatch_id=$1 AND state=<expected>` (optimistic CAS). Concurrent writers are rejected with `0 rows updated` and re-read.

### 2.2 Step-by-step write log

| Step | Actor | Writes (atomic unit) | Durability guarantee |
|---|---|---|---|
| 1. POST /dispatches | Ingress | `INSERT dispatch(...) state='RECEIVED'` + `INSERT dispatch_payload(...)` in one txn; Postgres `synchronous_commit=on` to sync replica | Survives primary crash without sync replica loss |
| 2. Ack ingress | Ingress | Returns 202 with `dispatch_id` after commit | Client may safely retry on timeout (idempotency_key uniqueness) |
| 3. Enqueue | Ingress (post-commit hook) | `JS PUBLISH dispatch.created` (envelope = dispatch_id only); JetStream `replicas=3, storage=file, ack=explicit` | Survives 1 NATS node loss |
| 4. Classify | Router | Consumes `dispatch.created`; runs cost-routing logic; `UPDATE dispatch SET provider=$, routed_at=now(), state='ROUTED'` | CAS guards against double-routing |
| 5. Gate (if cost ≥ threshold) | Router | If `est_cost_usd > project.approval_threshold` → publishes `dispatch.approval.requested`, state→`APPROVAL_PENDING`; webhook to approver; **no** worker pull yet | Held indefinitely in Postgres; no JetStream TTL |
| 6. Approve | Approval Gate | `UPDATE dispatch SET approved_by=$, approved_at=now()`; publishes `dispatch.routed.{provider}` | Approval row written **before** publish (ordering guarantee) |
| 7. Worker pull | Provider worker | JetStream `fetch(timeout=2s)`; on receipt: `UPDATE dispatch SET state='IN_FLIGHT', worker_id=$, lease_expires_at=now()+5min` | Lease prevents duplicate execution; expired leases reclaimed every 30 s |
| 8. Provider call | Worker | HTTPS to provider; tokens streamed; on each completion chunk → in-memory buffer | n/a |
| 9. Completion write | Worker | One txn: `UPDATE dispatch SET state='COMPLETED', completed_at=now(), input_tokens=$, output_tokens=$, total_cost_usd=$` + `INSERT cost_ledger_event` (append-only) + S3 PUT of response body | All three writes in single txn except S3 (best-effort); S3 path stored in row |
| 10. JS ack | Worker | After txn commit → `js.ack(msg)` | If worker dies after txn commit but before ack, lease sweep re-delivers and worker idempotently no-ops (state already COMPLETED) |
| 11. Aggregate | Cost Aggregator | Every 5 s: `INSERT INTO cost_rollup_5s SELECT … FROM cost_ledger_event WHERE event_id > $last_seen` (advances watermark) | Watermark in Postgres; restart-safe |

### 2.3 Idempotency

- Client supplies `Idempotency-Key` header. `dispatch(idempotency_key, project_id)` is UNIQUE; second POST returns the first dispatch's id.
- Worker writes use `event_id = uuidv7()` and `INSERT … ON CONFLICT DO NOTHING` on `cost_ledger_event(event_id)`.

### 2.4 Restart durability (the "single-node restart" constraint)

| Failure point | Recovery mechanism |
|---|---|
| Ingress pod dies before commit | Client sees 5xx, retries with same `Idempotency-Key` |
| Ingress pod dies after commit, before JS publish | Postgres `dispatch_outbox` table + outbox poller (5 s tick) republishes any row where `published_at IS NULL` |
| Router pod dies mid-classify | JetStream redelivers after `ack_wait=30s`; CAS on `state='RECEIVED'` prevents double-route |
| Worker pod dies mid-call | Lease expiry (5 min); lease sweeper republishes; worker re-issuing the provider call uses provider-side idempotency where available (Anthropic supports `Idempotency-Key`; DeepSeek/Kimi: we dedup by stored `request_hash` and skip if already COMPLETED) |
| Postgres primary crash | Patroni promotes sync replica (RPO=0, RTO≈15 s); WAL archived to MinIO every 60 s for PITR |
| NATS node crash | Stream replicas=3, R=2 quorum; survives 1 node loss; messages durable on remaining nodes |

---

## 3. Cost Routing Logic

### 3.1 Inputs to the router

```
RouteInput = {
  dispatch_id, project_id, task_type ∈ {code_gen, code_review, refactor,
                                        documentation, debugging, design,
                                        translation, classification, summarization},
  est_input_tokens, est_output_tokens,
  latency_slo_ms ∈ {500, 2000, 30000},      # tight / normal / batch
  required_capabilities: set,                # e.g. {tool_use, vision, 200k_context}
  project_budget_remaining_usd_today,
  project_budget_envelope_usd_today,
  provider_health: {claude, deepseek, kimi} → {ok, degraded, down, throttled}
}
```

### 3.2 Routing decision (deterministic, in this order)

**Step A — Capability filter.** Drop any provider missing a required capability.
- Vision → Claude only.
- ≥150 K context → Claude or Kimi (Kimi 2.6 supports 256K).
- Strict JSON mode + tool_use round-trip → Claude or DeepSeek.

**Step B — Latency SLO filter.** Drop providers whose 1-hour p95 exceeds `latency_slo_ms`.
- 500 ms SLO (interactive): typically only Claude qualifies.
- 2 000 ms SLO: all three usually qualify.
- 30 000 ms SLO (batch): all three; price dominates.

**Step C — Cost-optimal selection from survivors.**

Estimated cost per provider:
```
cost_claude   = in*$3   + out*$15      # per MTok, divide by 1e6
cost_deepseek = in*$0.14 + out*$0.87
cost_kimi     = in*$0.60 + out*$4.00
```

Routing table (default policy, per task_type):

| task_type | Primary | Fallback chain | Notes |
|---|---|---|---|
| code_gen (≤2K out) | DeepSeek | Kimi → Claude | DeepSeek-Coder strong, 6× cheaper than Kimi |
| code_review (citation-heavy) | Claude | DeepSeek (with stricter rubric) | Claude tool_use stable |
| refactor | DeepSeek | Claude | Cheap path acceptable |
| documentation | Kimi | DeepSeek → Claude | Kimi long-context wins |
| debugging | Claude | DeepSeek | Claude best at multi-hop reasoning |
| design | Claude | Kimi → DeepSeek | Long-form structured reasoning |
| translation (NL ↔ EN) | DeepSeek | Kimi | DeepSeek multilingual sufficient |
| classification / summarization | Kimi | DeepSeek | Volume task, prefer cheap |

**Step D — Budget envelope guard.** If routing to provider P would cause `project_spend_today + est_cost > project_budget_envelope * 0.95`, demote to next provider in the fallback chain. If only Claude survives capability+SLO and budget is exhausted → state=`FAILED_TERMINAL` with reason `budget_exhausted`, emit `budget.violation` event.

**Step E — Approval gate.** If `est_cost_usd > project.approval_threshold_usd` (default $1.00, configurable per-project), state=`APPROVAL_PENDING`. Operator webhook (Slack/email) with one-click approve link.

### 3.3 Fallback chain (runtime failures)

When the chosen provider returns 5xx, rate-limit (429 sustained >30 s), or times out:

```
attempt 1: primary           (Step C choice)
attempt 2: cheapest survivor that passes Step A+B
attempt 3: most-capable survivor (price-blind)
attempt ≥4: state = FAILED_RETRYABLE → exponential backoff (10 s, 40 s, 160 s)
attempt 7: state = FAILED_TERMINAL, alert ops
```

Each attempt records `attempt_n` in `dispatch_attempts` table for post-hoc cost reconciliation (failed attempts that consumed input tokens still count toward the ledger).

### 3.4 Expected cost mix (modeled)

Assuming task distribution 30% code_gen, 15% review, 15% refactor, 10% docs, 10% debug, 5% design, 5% translate, 10% classify/summary:

| Provider | Share | Daily token volume (est) | Daily cost |
|---|---|---|---|
| Claude | 18% | 1.26 B | $2 520 |
| DeepSeek | 58% | 4.06 B | $1 462 |
| Kimi | 24% | 1.68 B | $1 680 |
| **Total** | 100% | 7.0 B | **$5 662/day** |

vs. all-Claude baseline $45 000/day → **87% cost reduction**.

---

## 4. Failure Modes (≥5)

### 4.1 Provider rate-limit storm (Claude 429s sustained)

- **Detection**: `provider_429_rate_5m{provider="claude"} > 5%` Prometheus alert; worker-side circuit breaker trips at 50% over 30 s.
- **Recovery**: Circuit breaker opens for 60 s, then half-open with 1 probe/s. Router demotes Claude in capability-filter Step A (`provider_health=throttled`) for 5 min; dispatches with `required_capabilities ⊇ {claude_only}` queue in `approval_pending_throttle` rather than fail.
- **Blast radius**: Claude-only workloads (~18%) delayed up to 5 min; other 82% unaffected. No data loss — JetStream holds messages.

### 4.2 Postgres primary failure (single-node restart)

- **Detection**: Patroni health check fails twice in 5 s → leader election. Prometheus `pg_up{role="primary"} == 0`.
- **Recovery**: Sync replica promoted (RPO=0, RTO ≈ 15 s). Connection pools (PgBouncer) reconnect via VIP. Outbox poller resumes; lease sweeper resumes; no in-flight dispatch is lost because (a) ingress writes were sync-replicated, (b) JetStream still holds the message until worker acks.
- **Blast radius**: 15 s of write unavailability — ingress returns 503; clients retry with idempotency key. Zero data loss.

### 4.3 NATS node loss

- **Detection**: `nats_jetstream_cluster_size < 3` alert; meta leader re-election within 2 s.
- **Recovery**: Stream R=3 file-backed; loss of 1 node leaves R=2 quorum. Auto-replenishment when node returns (RAFT log replay). If a second node fails simultaneously → stream goes read-only; ingress falls back to writing `dispatch_outbox` only, polling resumes once quorum is back.
- **Blast radius**: Single-node loss = zero impact. Two-node loss = ingress degraded to outbox-only, additional latency ~5 s/dispatch until recovery, but no data loss.

### 4.4 Worker crash mid-call (provider call already billed)

- **Detection**: Lease expires (5 min) without state transition; lease sweeper detects `state='IN_FLIGHT' AND lease_expires_at < now()`.
- **Recovery**: Sweeper republishes to JetStream with `attempt_n += 1`. Worker on second attempt looks up `dispatch_attempts(dispatch_id, last_attempt)`; if `request_hash` matches and provider supports idempotency keys, reuses key (Anthropic) — no double billing. For DeepSeek/Kimi without idempotency, second call is made and **both attempts are recorded** in cost ledger (cost-correctness > cost-savings).
- **Blast radius**: Up to one duplicate provider call per crashed worker × dispatches in flight (≤ worker concurrency). At Kimi worker = 6 concurrent, max 6 duplicates per crash. Daily duplicate-call budget capped at 0.1% (1 000 dispatches); SLO violation if exceeded.

### 4.5 Cost ledger drift (>2% per provider per day)

- **Detection**: Daily reconciler runs at 02:00 UTC: pulls provider invoices/usage API for previous day; compares against `cost_ledger_event` sum per provider. Alert if `abs(invoice - ledger) / invoice > 0.02`.
- **Recovery**: Reconciler emits `cost_ledger_correction` event with delta; cost rollups regenerated for affected day. Root-cause investigation: usually (a) workers committed cost row but provider call was retried client-side without retry-marker, or (b) failed-attempt cost not recorded.
- **Blast radius**: Budget envelope calculations off by <2% for ≤24 h; project_budget_remaining recomputed after correction. No dispatch impact.

### 4.6 Approval queue starvation (cost-gated dispatches piling up)

- **Detection**: `dispatch_state{state="APPROVAL_PENDING"} > 100` for >1 h.
- **Recovery**: Page on-call; per-project escalation policy auto-approves dispatches under `auto_approve_after_minutes` (default 60) up to `auto_approve_ceiling_usd` (default $5). Above ceiling, dispatches remain pending and project owner is paged.
- **Blast radius**: Per-project; other projects unaffected. Worst case dispatch held 60 min then auto-resolved or rejected with `expired_approval`.

### 4.7 Disk full on JetStream node

- **Detection**: `node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.15` alert at 85% utilization.
- **Recovery**: Stream max-bytes set to 50 GB per stream (covers ~6 h of envelope volume at peak). Old delivered+acked messages auto-purged. At 85% utilization, autoscaler attaches +500 GB volume; if cluster-wide, ingress sheds load by returning 429 to non-priority projects (priority tier defined per-project).
- **Blast radius**: At 95% full without intervention, new publishes fail → ingress returns 503; clients retry. No data loss for already-published messages.

---

## 5. SLOs

### 5.1 Latency targets per tier (end-to-end: POST /dispatches → 200 OK with body)

| Tier | p50 | p95 | p99 | Throughput floor | Notes |
|---|---|---|---|---|---|
| Interactive (Claude, 2K out) | 1.2 s | 3.5 s | 7 s | 5 rps sustained | tighter than provider raw p95 by 15% (our overhead = +180 ms) |
| Normal (DeepSeek, 2K out) | 1.8 s | 5 s | 12 s | 20 rps sustained | dominant path; provider p95 ~4.2 s |
| Batch (Kimi, 4K out, async) | 4 s | 15 s | 30 s | 10 rps sustained | async completion via webhook acceptable |

**Internal-only latency budget (POST commit → JS publish)**: p99 ≤ 50 ms. Anything more means Ingress is the bottleneck.

### 5.2 Throughput floor

- **Floor**: 35 dispatches/sec sustained for 1 h without SLO violation.
- **Ceiling tested**: 58 dispatches/sec for 10 min before back-pressure kicks in.
- **Capacity headroom alert**: if `peak_rps_15m > 0.7 * tested_ceiling` → autoscale + page capacity-owner.

### 5.3 Error budgets (monthly, 30-day window)

| SLI | Target | Monthly error budget |
|---|---|---|
| Ingress availability (2xx/total ex-4xx) | 99.95% | 21.6 min |
| Dispatch completion within tier p99 | 99.0% | 1% of dispatches = 10K/day max |
| Cost ledger accuracy vs invoice | 98% (±2%) | reconciliation gap closed within 24 h |
| Approval-gate response time (operator action → dispatch resumed) | p95 ≤ 5 min in business hours | breach = page project owner |

### 5.4 SLO violation detection

- **Prometheus + Alertmanager**: every SLI emits a Prometheus metric (`dispatch_e2e_latency_seconds` histogram with `provider`, `tier` labels; `dispatch_state_total` counter; `cost_ledger_drift_ratio` gauge).
- **Burn-rate alerts**: 2× standard Google SRE multi-window-multi-burn-rate alerts:
  - Page: `(error_rate over 1h) > 14.4 × budget AND (error_rate over 5m) > 14.4 × budget` → 2% budget burn in 1 h.
  - Ticket: `(error_rate over 6h) > 6 × budget AND (error_rate over 30m) > 6 × budget` → 5% burn in 6 h.
- **Synthetic probes**: 1 dispatch per provider per minute (`probe_project_id`); separate SLO so probes catch issues even when real traffic dips.
- **Dashboards**: Grafana board per tier (`Interactive`, `Normal`, `Batch`) showing p50/p95/p99, error rate, budget burn-down. One row per project for noisy-neighbor detection.

---

## 6. Storage schema (canonical tables, abridged)

```sql
CREATE TABLE dispatch (
  dispatch_id        UUID PRIMARY KEY,                  -- uuidv7 (time-ordered)
  project_id         TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL,
  state              TEXT NOT NULL,                     -- enum, see §2.1
  task_type          TEXT NOT NULL,
  provider           TEXT,                              -- set at ROUTED
  worker_id          TEXT,
  attempt_n          SMALLINT NOT NULL DEFAULT 0,
  est_cost_usd       NUMERIC(10,6),
  total_cost_usd     NUMERIC(10,6),
  input_tokens       INT,
  output_tokens      INT,
  approval_required  BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by        TEXT,
  approved_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  routed_at          TIMESTAMPTZ,
  in_flight_at       TIMESTAMPTZ,
  completed_at       TIMESTAMPTZ,
  lease_expires_at   TIMESTAMPTZ,
  state_changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, idempotency_key)
);
CREATE INDEX dispatch_state_idx     ON dispatch (state) WHERE state IN ('RECEIVED','ROUTED','APPROVAL_PENDING','IN_FLIGHT');
CREATE INDEX dispatch_lease_idx     ON dispatch (lease_expires_at) WHERE state = 'IN_FLIGHT';
CREATE INDEX dispatch_project_day   ON dispatch (project_id, (created_at::date));

CREATE TABLE dispatch_outbox (             -- exactly-once enqueue from Ingress
  dispatch_id   UUID PRIMARY KEY REFERENCES dispatch,
  published_at  TIMESTAMPTZ
);

CREATE TABLE cost_ledger_event (           -- append-only
  event_id        UUID PRIMARY KEY,         -- uuidv7
  dispatch_id     UUID NOT NULL,
  attempt_n       SMALLINT NOT NULL,
  provider        TEXT NOT NULL,
  input_tokens    INT NOT NULL,
  output_tokens   INT NOT NULL,
  cost_usd        NUMERIC(10,6) NOT NULL,
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX cost_ledger_provider_day ON cost_ledger_event (provider, (recorded_at::date));

CREATE TABLE cost_rollup_5s (              -- materialized by Cost Aggregator
  bucket_start_ts TIMESTAMPTZ NOT NULL,
  provider        TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  cost_usd        NUMERIC(12,6) NOT NULL,
  dispatch_count  INT NOT NULL,
  PRIMARY KEY (bucket_start_ts, provider, project_id)
);
```

Daily rollup (`cost_rollup_daily`) is built from `cost_rollup_5s`. The ±2% reconciliation runs against `cost_ledger_event` directly (immutable source of truth); rollups are recomputable.

---

## 7. Capacity & cost summary (the bottom line)

| Resource | Sizing | Monthly cost (rough, on-prem or cheap cloud) |
|---|---|---|
| Postgres HA (3 nodes, 16/64/NVMe) | 48 vCPU / 192 GB RAM | $1 200 |
| NATS JetStream (3 nodes) | 24 vCPU / 48 GB / 1.5 TB | $600 |
| Worker pods (mixed) | ~80 vCPU at peak | $1 500 |
| MinIO 4×4TB | 16 TB raw | $400 |
| Observability stack | 8 vCPU / 32 GB / 2 TB | $300 |
| **Infra subtotal** | | **$4 000/mo** |
| **Provider spend (modeled)** | 7 B tok/day, mixed | **$170 000/mo** |
| Total | | $174 000/mo for 30 M dispatches/mo = **$0.0058 per dispatch** |

vs. naive all-Claude: $1.35 M/mo = $0.045 per dispatch. **Cost routing saves $1.18 M/mo at this scale.**

---

## 8. What's intentionally not in v1

- No multi-region active-active (single-region with HA only; DR is restore-from-WAL).
- No fine-tuned in-house fallback model.
- No streaming-to-client for interactive tier (added in v1.1 if interactive p95 needs to drop below 1.5 s).
- No dynamic prompt-compression router (could shave another 15-20% on input tokens — v1.2).
