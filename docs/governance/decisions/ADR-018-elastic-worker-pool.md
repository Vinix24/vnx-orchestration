# ADR-018: Elastic worker pool (Wave 6)

- **Status**: Accepted
- **Date**: 2026-05-16
- **Authors**: Vincent van Deth
- **Supersedes**: none
- **Cross-refs**: ADR-011 (worker isolation), ADR-013 (multi-worker design),
  ADR-017 (Control Centre as agent role)

## Context

VNX runs four canonical terminals (T0-T3) with fixed roles. Multi-project supervision
(Wave 5) and provider diversity (Wave 7 — DeepSeek/Kimi/GLM) created pressure for
more workers per project, mixed providers per pool, and elastic scaling.

ADR-013 (2026-05-09) designed Workers=N as a future direction but stayed design-only.
Wave 6 implements ADR-013 with four binding decisions.

**Concrete pressure points that forced this decision:**

1. Fixed pool does not fit load profiles. `sales-copilot` has one worker to spare;
   `seocrawler-v2` sits with 47 open items and 8 P0s against the three-worker ceiling.
2. No elasticity on queue pressure. When `mission-control` goes 8 pending dispatches
   deep, there is no mechanism to temporarily route six workers to it.
3. Provider-mix requires loose pools. Wave 4.6 spawn handlers (claude, codex, gemini,
   litellm) make per-pool provider composition possible; today every `Tn` implies one
   provider.

**Fundering already shipped (2026-05-16 inventory):**

| Component | Location | Status |
|---|---|---|
| `claude_spawn` | `scripts/lib/provider_spawns/claude_spawn.py` | PR #490 merged |
| `codex_spawn` | `scripts/lib/provider_spawns/codex_spawn.py` | Wave 4.6 merged |
| `gemini_spawn` | `scripts/lib/provider_spawns/gemini_spawn.py` | Wave 4.6 merged |
| `litellm_spawn` | `scripts/lib/provider_spawns/litellm_spawn.py` | Wave 4.6 in flight |
| Multi-tenant lease isolation | `schemas/migrations/0017_multi_tenant_lease_isolation.sql` | Wave 5 merged |
| Worker health monitor | `scripts/lib/worker_health_monitor.py:30-46` | shipped |
| Lease expiry + audit-event | `scripts/lib/lease_manager.py:320-360` | shipped |
| Post-exit cleanup (idempotent) | `scripts/lib/cleanup_worker_exit.py` | shipped |

What is missing: pool registry, schema v13, PoolManager, scaling policies, provider-mix
binding, health-aware reap, operator CLI, Control Centre integration — eight PRs total
(PR-6.1 through PR-6.8).

## Decision

### Rule 1: Pools are per-project; cross-pool transfer rejected

Each project has its own pool of N workers. A worker is bound to its pool for the
duration of its lifetime. Cross-pool worker transfer (moving a worker from
project-A's pool to project-B's pool) is rejected:

- Cross-project audit trail integrity (ADR-005)
- Provider-key isolation (project-A's DEEPSEEK_API_KEY scope ≠ project-B)
- Smart-context cache per project — invalidation on transfer too complex

Failure mode FM-4 (cross-pool contamination) is mitigated by the dispatch project_guard
shipped in Wave 5 PR-5.1: workers validate `dispatch.project_id == own_lease.project_id`
before accepting any dispatch.

### Rule 2: PoolManager decides within T0-tick, no long-running loop

PoolManager runs as a pure function inside T0's regular tick cycle. No background
thread, no autonomous scaling loop. Operator + ledger remain the orchestration
substrate (ADR-005, per memory `project_vnx_state_mediated_coordination`).

```python
# In T0's tick():
state = read_state()
queue = read_pending_dispatches()
decision = pool_manager.decide(state, queue)   # pure function
apply_decision(decision)                       # spawn/reap via Wave 4.6 handlers
```

`PoolManager.decide()` is a pure function with no I/O — it reads a snapshot and
returns a `PoolDecision(action, delta, reason, targets, cooldown_remaining_s)`. The
I/O happens in `execute()`, called separately by the T0-tick. This makes the decision
logic unit-testable in full isolation.

### Rule 3: Spawn mechanism reuses Wave 4.6 handlers unchanged

Pool scaling does not introduce new spawn paths. It reuses:
- `scripts/lib/provider_dispatch.py` for provider routing
- `scripts/lib/provider_spawns/{claude,codex,gemini,litellm}_spawn.py` for spawn
- `scripts/aggregator/t0_lifecycle.py` for lifecycle state machine

New code in Wave 6 owns: pool composition, allocation policy, reap policy. All four
Wave 4.6 spawn handlers follow identical pure-spawn-and-stream slice; the caller
(`subprocess_dispatch.py`) already handles lease, manifest, receipt, archive, retry.
Wave 6 pool spawns hook in at the caller level — the handlers themselves are untouched.

### Rule 4: Backward compatibility via aliases

77 call-sites hardcode `T1/T2/T3`. Default `.vnx/vnx_workers.default.yaml` ships
with `aliases: [T1, T2, T3]` so existing code resolves the same workers. New
pools opt into elastic scaling explicitly.

Migration path:
- Phase 1 (PR-6.1): registry shipped, default yaml provides aliases
- Phase 2 (PR-6.2): schema v13 adds pool tables (`pool_config`, `worker_pools`,
  `worker_pool_membership`)
- Phase 3 (PR-6.3-6.6): PoolManager + scaling + provider-mix + reap
- Phase 4 (PR-6.7+): operator CLI + Control Centre integration

The 77 hardcoded sites are migrated incrementally. Three critical entry points go first:
`runtime_facade.CANONICAL_TERMINALS` (`:49`), `decision_executor.TERMINAL_TO_TRACK`,
and `vnx_doctor_checks.expected_terminals`. The remaining 74 long-tail sites follow
after Wave 6 MVP — not blocking.

## Architecture

```
+----------------------------------------------------------------+
|              Control Centre (Wave 5, interactive)              |
|  reads: ~/.vnx-aggregator/data.db.pool_state_unified           |
|  writes: ~/.vnx-aggregator/events/pool_decisions.ndjson        |
+----------------------------------+-----------------------------+
                                   |
                                   v
+----------------------------------------------------------------+
|        Per-project T0 (Wave 5, headless per-turn fresh)        |
|  on tick: read pending queue, ask PoolManager, emit decisions  |
+----------------------------------+-----------------------------+
                                   |
                                   v
+----------------------------------------------------------------+
|            PoolManager (Wave 6 PR-6.3, in-process)             |
|  - decide(queue_depth, current_workers, health) -> PoolDecision|
|  - scale_up(n) -> spawn via provider handler                   |
|  - scale_down(n) -> release lease + membership                 |
|  - reap_dead() -> cleanup_worker_exit + membership delete      |
+----+------------------------------+----------------------------+
     |                              |
     v                              v
+--------------------+   +---------------------------------------+
| runtime_coord v13  |   |  provider_spawns/{claude,codex,       |
|  - worker_pools    |   |  gemini,litellm}_spawn.py             |
|  - pool_config     |   |  (Wave 4.6, unchanged)                |
|  - pool_membership |   +---------------------------------------+
|  - terminal_leases |
|  - worker_states   |
+--------------------+
```

## Consequences

### Positive

- Provider-mix per pool enables cost-routing without manual orchestration. A
  `seocrawler-v2` pool can run `["claude","claude","codex"]` — impl-heavy with one
  gate-runner — without touching the `sales-copilot` pool.
- Queue-aware scaling reduces idle worker cost. Default policy:
  `target = clamp(min_workers, ceil(queue_depth/2), max_workers)`.
- Per-project pool isolation keeps audit trail clean — FM-4 (cross-pool contamination)
  is structurally impossible when pool membership is bounded by `project_id`.
- End-state: 4 deployments at [1-4], [2-6], [1-3], [2-8] + optional codex-pool [1-2]
  = up to 19 parallel workers, versus today's fixed 12.

### Negative

- New schema migration (v13, migration 0018) adds operational risk. Four separate
  `runtime_coordination.db` instances need rolling migration.
- 77 hardcoded `T1/T2/T3` references require alias-translation layer until migrated.
- PoolManager.decide() and pool state model add complexity to T0-tick.

### Mitigations

- Backward-compat aliases ship in default yaml (Rule 4); existing dispatches, receipts,
  NDJSON archives are not rewritten.
- `PoolManager.decide()` is a pure function — unit testable in isolation via
  `pool_decision_engine.py` (no I/O dependency).
- Per-project pool isolation limits blast radius of a bad decision to one project.
- Schema v13 rollback: drop `pool_config`, `worker_pools`, `worker_pool_membership`
  + decrement `runtime_schema_version` to 12.
- Cooldown 120s + hysterese guard prevents scale oscillation (FM-6: requires
  `queue==0 AND idle>min` for 60s before scale-down triggers).

## Acceptance criteria for Wave 6

Each subsequent PR (6.1 through 6.8) lists its own acceptance criteria. Wave 6 as a
whole is "done" when:

1. Default install still works: 4 workers T0-T3 via aliases, no changed behavior
2. Operator can opt into 6-worker pool with provider-mix via `vnx_workers.yaml` edit
3. Queue-aware scaling demonstrably reduces idle worker count (demo: burst of 10
   pending in `seocrawler-v2` → pool scales 2→6 within 30s → drained in 8min → back
   to 2)
4. Schema v13 migration is reversible (down-migration tested)
5. `vnx pool status` shows 4 pools across 4 projects
6. ADR-018 cross-linked from ADR-011, ADR-013, ADR-017

## Rejected alternatives

- **Threads instead of subprocesses**: violates ADR-011 worker isolation. Each worker
  must be a separate process with its own dispatch lifecycle and receipt.
- **Cross-project worker pool**: violates audit-trail integrity (Rule 1). FM-4
  contamination is not mitigatable without structural project isolation.
- **Long-running PoolManager daemon**: violates state-mediated coordination model.
  T0 is per-turn-fresh; a daemon would add process continuity that breaks the
  operator + ledger substrate invariant.
- **Provider switching mid-worker-lifetime**: provider-binding is immutable at spawn.
  Switching requires release + respawn — simpler than in-place provider migration.
- **Auto-spawning workers on demand per dispatch**: rejected (echoes ADR-013). Pool
  size is a deployment-level decision, not a per-dispatch one. Consistent with
  ADR-006's mandatory human gate.

## Failure mode register (summary)

| ID | Failure mode | Mitigation |
|---|---|---|
| FM-1 | Pool-leak after T0 crash | Atomic lease+membership in one SQLite transaction |
| FM-2 | Lease-stranding after cleanup | tick() startup reconciles `released_at IS NULL` rows against `terminal_leases.state` |
| FM-3 | Quota-burst (all N workers hit rate-limit simultaneously) | reap_dead detects clustered failure; pool transitions `state='quota_exhausted'` |
| FM-4 | Cross-pool contamination | Dispatch project_guard rejects `dispatch.project_id != own_lease.project_id` |
| FM-6 | Scale oscillation | Cooldown 120s + hysterese (`queue==0 AND idle>min` for 60s required) |
| FM-8 | Concurrent migration 0018 | `BEGIN IMMEDIATE` with 30s timeout; per-project DBs are physically separate |
| FM-10 | Membership race | Partial unique index `idx_pool_membership_active` blocks second active row |

Full failure mode register (FM-1 through FM-12) in `claudedocs/wave6-workers-n-architecture.md` §9.

## References

- `claudedocs/wave6-workers-n-architecture.md` — full Wave 6 design (schema, PR breakdown,
  sequencing, operator interaction model, failure modes, timeline)
- ADR-011 — worker isolation (process-per-worker, depth>1 hierarchy)
- ADR-013 — workers=N as configuration (design-only predecessor; implemented by Wave 6)
- ADR-015 — Wave 7 LiteLLM Path B (provider-mix per pool uses LiteLLM-routed providers)
- ADR-016 — Unified event shape (pool receipts use CanonicalEvent across all providers)
- ADR-017 — Control Centre as agent role (Wave 5 supervisor; extended by PR-6.8)
- `scripts/lib/runtime_facade.py:49` — `CANONICAL_TERMINALS` constant (replaced by
  `WORKER_REGISTRY` in PR-6.1)
- `schemas/migrations/0017_multi_tenant_lease_isolation.sql` — Wave 5 multi-tenant
  foundation that Wave 6 schema v13 extends
