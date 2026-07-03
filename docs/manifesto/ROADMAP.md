# VNX Roadmap

> **This file is the architecture-principles + wave-history roadmap.** Live, per-feature status lives in the maintainer's tracks database (the `vnx objective` CLI); the repo-root [`ROADMAP.yaml`](../../ROADMAP.yaml) is a generic example of the machine-readable roadmap format, not the live plan. Some milestone stamps below are historical (written April–May 2026) and may lag the current tree; trust [`ROADMAP.md`](../../ROADMAP.md) and the README for what is actually shipped.

**Status**: Public roadmap  
**Planning Horizon**: 2026 (rolling)  
**Principle**: Governance-first, model-agnostic, local-first

---

## How to Read This Roadmap

- `Completed`: Shipped and merged.
- `Committed`: Actively planned for near-term implementation.
- `Next`: High-value follow-up after committed scope lands.
- `Exploring`: Valid experiments, lower priority, or needs more validation.

VNX remains a governance-first system. Features that reduce human oversight are evaluated carefully and are never default behavior.

---

## Recently Completed

### F37: Auto-Report Pipeline
**Status**: `Completed` — March 2026

Stop hook → deterministic extraction → haiku classification → markdown report. Workers no longer manually assemble reports. `VNX_AUTO_REPORT=1` activates the pipeline.

Key deliverables: `stop_hook.py`, `report_assembler.py`, `haiku_classifier.py`, `VNX_AUTO_REPORT` feature flag.

### F38: Dashboard Unified
**Status**: `Completed` — April 2026

Single dashboard for coding and business domains. Domain filter tabs, session history browser, agent selector by name, reports browser surface.

Key deliverables: unified dashboard UI, domain tabs, session history, agent name selector.

### F39: Headless T0 Benchmark
**Status**: `Completed` — April 2026

Decision framework rewrite + gate locks + replay harness. Deterministic pre-filter handles ~70% of decisions without LLM. Benchmark baseline: Level-1 100%, Level-2 73–87%, Level-3 67–78%.

Key deliverables: `t0_decision_framework.py`, `t0_gate_locks.py`, `t0_context_assembler.py`, `t0_replay_harness.py`. Taxonomy simplified to DISPATCH/COMPLETE/WAIT/REJECT/ESCALATE.

---

## Wave History

Wave-based planning tracks major capability milestones across the 2026 roadmap. All waves below are shipped and merged.

### Wave 1 — Shadow Read Cutover
**Status**: `Completed` — April 2026

Central VNX state shadow-mode validation. `shadow_verifier.py` computes 6 zero-tolerance metrics (wrong-project rows, scoping/blocking-finding mismatch, top-3 divergence, count drift, lease-key collisions, p95-latency ratio). NDJSON divergence records under flock with timestamp-suffix rotation. All production T0 + IntelligenceSelector read sites wired through `VNX_USE_CENTRAL_DB` flag.

### Wave 2 — Package Extraction Foundation
**Status**: `Completed` — April 2026

`pyproject.toml` + `vnx_core` + `vnx_cli` package skeleton with smoke tests. First module migration: `function_size_gate.py` → `vnx_core`. Pipx-installable wheel foundation (`vnx` console_script entry-point).

### Wave 4 / 4.5 / 4.6 — Provider Generalization
**Status**: `Completed` — April/May 2026

OTel observability foundation (opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`). `PromptAssembler` provider-agnostic methods for Claude/Codex/Gemini/LiteLLM. `provider_dispatch.py` entry-point with per-provider spawn handlers (`claude_spawn`, `codex_spawn`, `gemini_spawn`, `litellm_spawn`). `CanonicalEvent` unified event shape via `EventStore` enforcement.

### Wave 5 — Control Centre + Multi-Project
**Status**: `Completed` — SHIPPED 2026-05-16

Single supervisor session managing N per-project T0 orchestrators. Multi-project state aggregator, per-project T0 lifecycle (spawn/heartbeat/kill/reap), multi-tenant lease isolation (schema v12), cross-project intelligence aggregator, hybrid dispatch routing, operator demo runbook.

Key deliverables: `multi_project_aggregator.py`, `control_centre_skill.md`, `CONTROL_CENTRE.md`, ADR-017.

### Wave 6 — Workers=N Elastic Pool
**Status**: `Completed` — SHIPPED 2026-05-16

Configurable worker pool with queue-aware + cost-aware scaling policies, dead-worker reap, and `vnx pool` CLI. PoolManager core (schema v14), pluggable scaling policies (`queue_depth_v1` + `cost_aware_v1`), provider-mix per pool, health monitoring + dead-worker reap tick cycle.

Key deliverables: `pool_manager.py`, `vnx_workers.yaml`, ADR-018.

### Wave 7 — Multi-Provider via LiteLLM
**Status**: `Completed` — SHIPPED 2026-05-17

Five providers in production: Claude, Codex CLI, Gemini CLI, Kimi CLI (OAuth), LiteLLM bridge (DeepSeek V4-Pro/V4-Flash, GLM-5.1 via OpenRouter). Uniform receipt + report shape across all 5 providers. Intelligence injection, token + cost tracking, and quality gates equal first-class for every provider. Reproducible benchmark suite: 9 models × 7 tasks with routing recommendations.

Key deliverables: `litellm_spawn.py`, `provider_governance.py`, `vnx.env`, `routing_recommendations.yaml`, ADR-015.

### Wave 8 — Smart Router + Schema Enforcement
**Status**: `Completed` — SHIPPED 2026-05-17

Task-class-aware model routing (`smart_router.py`), hard provider constraint enforcement (`HardConstraintViolation` on policy breach), YAML frontmatter guardrails for uniform report schema, weekly drift + monthly benchmark cadence, self-learning `route_decisions_watcher.py` auto-adjusting `routing_recommendations.yaml` on production failure patterns.

Key deliverables: `smart_router.py`, `hard_constraints.py`, `route_decisions_watcher.py`.

### 1.0 Sprint — Roadmap Autopilot, N-Scaling Foundation, Auto-Dream
**Status**: `Completed` — SHIPPED 2026-05-29

~40 PRs completing the 1.0 capability surface. Major additions:

**Roadmap Autopilot (RA-1..6 + RA-3b)**
Gate-enforced autopilot primitives for the roadmap advance flow. RA-1 stamps `project_id` on `roadmap_state.json` and receipts (ADR-007 compliance). RA-2 provisions branch + worktree on `load_feature`. RA-3 enforces review-gate evidence in reconcile/advance. RA-3b closes 4 advance-gate bypass holes. RA-4 adds a human-approval gate primitive. RA-5 adds a `step` subcommand driving the active feature PR queue. RA-6 wires `autopilot_tick` + scheduler — ships dark (default off), gated by `VNX_ROADMAP_AUTOPILOT=1`.

**N-Scaling Foundation (N-1/2/3)**
N-1: atomic `claim_next_queued_dispatch` + migration 0026 for race-free queue consumption. N-2: `pool_worker_runner` single-claim entrypoint. N-3: `VNX_POOL_TASK_CONSUMER` wiring from pool worker spawn. Full pool-task-consumer path is opt-in; single-worker default unchanged.

**Auto-Dream Self-Learning Loop (runnable, not fully-active)**
ADR-019 `auto_dream` consolidator core: quality DB consolidation, CLI + scheduler, GAP-7 receipt preflight, T0 review-gate, kimi timeout guard, canonical vnx_paths I/O routing. The loop is runnable on-demand. Nightly cron trigger and central-path unification are pending before routine activation. Do not claim as "active" — it is runnable.

**GOV-1 PreToolUse Hook**
Blocks raw `claude` worker spawns at the Claude Code hook layer, enforcing the `subprocess_dispatch` path.

**Smart Routing Activation**
Full smart-router wired for non-Claude providers + constraint-aware routing (#709). Cost-aware auto-route activated via `VNX_AUTO_ROUTE=1` (#702). Opt-in; not the default dispatch path.

**Worktree + Provider Isolation**
Per-dispatch git worktree isolation (`VNX_ISOLATED_WORKTREE`, default off) extended to `provider_dispatch` for all providers.

**Track Layer (FUT-1 + FUT-2)**
`tracks` schema with DAL + CLI + audit-ordering (FUT-1). ADR-007 composite PK over `track_id + project_id`, tenant-scoping, composite-FK from `dispatches.track → tracks(track_id, project_id)` (FUT-2a). Structural regression tests for all track child tables (FUT-2b). Parallel execution contract: `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md` (ADR-020, 2026-05-30).

**Intelligence + Enrichment**
Repo-map enrichment extended to all providers (#712). Kimi intelligence injection wired (#701). ADR FTS5 index + injection in dispatch context (INT-1, INT-2).

**Hygiene**
Vulture dead-code detection wired; 100%-confidence dead-code cleared. Universal cost tracking across all 5 providers. Wheel packaging hardened (exclude pycache/tests/benchmarks, remove stale dist/).

Key deliverables: `autopilot_tick.py`, `pool_worker_runner.py`, `auto_dream` package, `GOV-1` hook, `tracks` table + DAL, migration 0026.

---

## Milestones

### 1.0.0 Public Release
**Status**: `Released` — 2026-07-02. Published to PyPI (`pip install vnx-orchestration`); source tagged `v1.0.0`.

pip-installable, 5-provider production, governance receipts, elastic pool, smart routing (opt-in), roadmap-autopilot gate hardening (RA-1..5 active, RA-6 dark), auto-dream self-learning loop runnable. See [1.0 Sprint section](#10-sprint--roadmap-autopilot-n-scaling-foundation-auto-dream) for full capability list.

### Headless T0 Production
**Status**: `Planned`

Cutover from interactive T0 to autonomous headless T0 for standard feature chains. Requires benchmark scores above 85% at Level-3, plus 3-Layer Trigger System operational, plus RA-6 autopilot-tick promoted from dark.

Success criteria: T0 makes correct dispatch/complete/wait decisions autonomously across 10 consecutive real feature chains without operator override.

---

## Committed (Near Term)

## 1) Multi-Feature PR Queue
**Status**: `Completed` — Shipped via Wave 5 (multi-project state aggregator + per-project T0 lifecycle)
**Why**: Current flow is strong for one feature at a time, but throughput is limited.

**Goals**
- Support multiple active feature plans in one orchestration session.
- Keep dependency checks deterministic per feature and across features.
- Preserve clear ownership and review signals in T0.

**Success Criteria**
- T0 can list/select/manage multiple features safely.
- No cross-feature dispatch confusion.
- Queue state remains reconstructable from receipts + state files.

---

## 2) Smart Context Injection (Indexed Docs + Line Targets)
**Status**: `Completed` — Shipped via Wave 5/7 (three-state-aware context bundles + intelligence injection unification)
**Why**: Better context precision reduces hallucinations and prompt bloat.

**Goals**
- Index project docs and key code references.
- Inject context blocks with line-targeted references when possible.
- Keep token budget bounded and deterministic.

**Success Criteria**
- Smaller, more relevant dispatch payloads.
- Fewer context-related re-dispatches.
- Consistent reference format across supported terminals.

---

## 3) Codex Model Switching Hardening
**Status**: `Completed` — Shipped via Wave 4.6/7 (per-provider spawn handlers + provider behavior contracts)
**Why**: Model switching works functionally, but needs battle-tested reliability.

**Goals**
- Stabilize provider/model switching paths for Codex worker lanes.
- Strengthen error handling for command/profile mismatches.
- Improve observability of provider-specific failure modes.

**Success Criteria**
- Stable switching in repeated production-like runs.
- Clear failure receipts when model launch/switch fails.
- No regression in dispatch delivery or receipt append path.

---

## 4) Worktree-Aware Orchestration
**Status**: `Completed` — Shipped in v1.0.0-rc1 (worktree metadata in dispatch + receipt, cross-branch write prevention)
**Why**: Parallel PR execution needs branch/worktree isolation.

**Goals**
- Support git worktree mapping per terminal/task.
- Add worktree metadata to dispatch and receipt context.
- Prevent accidental cross-branch writes.

**Success Criteria**
- Parallel PR flows run without branch contamination.
- T0 can inspect terminal-to-worktree mapping at a glance.
- Recovery flows preserve worktree ownership.

---

## Next (After Committed Scope)

## Centralization Rollout
**Status**: Data centralization `Completed` for all 4 projects; runtime cutover ongoing.
**Why**: Wave 8 shipped; the multi-tenant central corpus is validated. All four projects' governance history now lives in the shared multi-tenant DB (`~/.vnx-data/state/`, `project_id`-stamped).

**Model**: the live runtime writes to the per-project central dir (`~/.vnx-data/<project_id>/`, resolved by `resolve_central_data_dir`); the shared corpus is the federation/analytics layer. ADR-007 composite-key compliance is enforced — the `nightly_digests` cross-tenant `digest_date` collision was the last gap, fixed 2026-06-20 (composite `UNIQUE(project_id, digest_date)` + the audit pattern now flags `*_date`).

**Rollout status (2026-06-20)**
1. vnx-orchestration (`vnx-dev`) — data migrated + verified (356,301 rows, non-destructive, `--verify-only` clean).
2. SEOcrawler_v2 — data migrated (727,101 code_snippets); runtime cutover to 1.0 + per-project central dir IN PROGRESS (selective-relocate plan, multi-LLM reviewed).
3. mission-control — data migrated (14,152 rows); runtime cutover queued (#2, all-providers gate).
4. sales-copilot — data migrated (37,652 rows); runtime cutover queued.

---

## 4-Gate Enforcement Framework
**Status**: `Next`
**Why**: Research complete in `claudedocs/4-gate-research-deep-dive-2026-05-18.md` + shift-left QA addendum. Triple gate (codex + gemini + CI) is validated; extending to a 4th deterministic gate with shift-left enforcement.

**Goals**
- Specify and implement the 4th gate (shift-left pre-dispatch quality signal)
- Deterministic enforcement — no LLM bypass path
- Integrate 4th gate verdict into structured result records with evidence binding
- Document rollback path for failed 4th gate

**Success Criteria**
- 4-gate policy enforced in CI for all new PRs
- Gate verdicts stored in `.vnx-data/state/review_gates/results/` with full evidence chain
- Shift-left addendum patterns adopted in dispatch pre-check

---

## Wave 9 — VNX-Dispatcher MVP
**Status**: `Next`
**Why**: Research complete in `claudedocs/vnx-dispatcher-strategic-research.md`. Next architectural milestone after centralization — standalone dispatcher replacing the shell-script dispatch loop.

**Goals**
- VNX-Dispatcher as a deployable standalone service
- Event-driven architecture for dispatch lifecycle (created → promoted → started → completed)
- First-class support for cross-project dispatch from a single dispatcher instance
- Backwards-compatible with existing `.vnx-data/dispatches/` directory structure

**Success Criteria**
- VNX-Dispatcher MVP deployable independently of the VNX CLI
- Dispatch + receipt events flow without loss through the dispatcher
- Drop-in replacement — existing `.vnx-data/` structure unchanged

---

## Recommended Next Hardening Chain
**Status**: `Next`
**Why**: The first four-feature autonomous run proved substantive delivery, but repeated governance gaps showed that formal closure and dispatch integrity still need a focused hardening lane before the next broad autonomous rollout.

**Recommended order**
1. Deterministic Headless Gate Evidence Enforcement
2. Terminal Input-Ready Mode Guard
3. Queue And Runtime Projection Consistency Hardening
4. Fine-Grained Delivery Rejection Logging
5. Residual Governance Bugfix Sweep

**Intent**
- First remove the repeated false-green closure path around headless gates.
- Then close the tmux input-mode corruption path for slash-prefixed dispatches.
- Then reconcile operator-visible queue/projected state with active runtime truth.
- Then make delivery failures diagnostically precise instead of generic rejects.
- Finally sweep the remaining warn-level governance bugs into a clean baseline for the next multi-feature autonomous chain.

---

## 5) Terminal Pool Expansion (4 -> N)
**Status**: `Completed` — Shipped via Wave 6 (Workers=N elastic pool with `vnx pool` CLI, schema v14)
**Why**: Higher throughput and specialization require dynamic terminal scaling.

**Goals**
- Move from fixed T1/T2/T3 lanes to a terminal pool.
- Support capability-aware assignment (provider/model/skill fit).
- Keep governance and status clarity as concurrency increases.

---

## 6) Dashboard V2
**Status**: `Next`  
**Why**: More terminals and features require richer operational visibility.

**Goals**
- Show explicit states like `working`, `waiting_for_input`, `blocked`, `done_unreviewed`, `done_approved`.
- Improve feature-level and queue-level visibility.
- Surface open-items and advisory posture directly in primary dashboard views.

---

## 7) Ledger Replay and Recovery Tooling
**Status**: `Next`  
**Why**: Replayability is core to auditability and crash recovery.

**Goals**
- Reconstruct queue and terminal state from receipts on demand.
- Provide drift detection between canonical files and replayed state.
- Ship operator-safe recovery commands for partial failures.

---

## 8) Schema Versioning for Dispatch/Receipt Contracts
**Status**: `Next`  
**Why**: Contract evolution needs explicit compatibility guarantees.

**Goals**
- Add versioned schemas for dispatch and receipt formats.
- Enforce compatibility checks in CI.
- Publish migration notes for breaking changes.

---

## 9) Refactoring and Simplification Sweep
**Status**: `Next`  
**Why**: Long-term reliability requires reducing complexity as features grow.

**Goals**
- Continue splitting large scripts into testable modules.
- Remove leftover legacy wrappers and dead paths where safe.
- Keep CLI behavior stable while improving maintainability.

## 10) Terminal Input-Ready Mode Guard
**Status**: `Next`
**Why**: Mouse-enabled tmux environments can leave a pane in copy/search mode, and slash-prefixed dispatches can then be swallowed by tmux itself.

**Goals**
- Detect `pane_in_mode` before dispatch.
- Recover safely when a pane can be returned to normal input mode.
- Fail closed when input readiness cannot be proven.
- Add certification that reproduces the real `search down` dispatch-corruption path.

**Success Criteria**
- Slash-prefixed dispatches are never sent blindly into a non-normal tmux mode.
- Recovery vs blocked delivery is explicit and auditable.
- The `search down` failure mode has a permanent regression test.

---

## Next (After Committed Scope — Governance Hardening Series)

### Gate Locks v2
**Status**: `Next`
**Why**: Gate locks currently cover codex/gemini review gates. Extend to CI green status, business compliance gates, and PR approval state.

**Goals**
- Lock source: pull gate status from GitHub API / CI webhook, not manual file writes.
- Compound lock support: require multiple gates cleared before COMPLETE is allowed.
- Lock expiry: time-bounded locks for gates that need periodic re-verification.

### 3-Layer Trigger System
**Status**: `Next`
**Why**: Headless T0 currently requires a polling loop. A proper trigger system allows event-driven wakeup with silent periods handled safely.

**Design**
- Layer 1: File watcher on `unified_reports/` — immediate trigger on new report arrival.
- Layer 2: Silence watchdog — cron every 10 min, deterministic checks (queue non-empty? receipts pending?).
- Layer 3: LLM triage — haiku invoked only when anomaly detected (stale dispatch, ambiguous receipt state).

**Why layered**: Layer 1 covers the normal case instantly. Layer 2 catches silent failures without burning LLM tokens. Layer 3 reserves expensive inference for genuine ambiguity.

### F40: Business Agent Integration
**Status**: `Next`
**Why**: Replace fragile n8n → SSH → MacBook → claude -p pipeline for VNX Digital workers.

**Goals**
- SubprocessAdapter on GCP VM for business-domain agents.
- Business-light governance profile: folder-scoped, review-by-exception.
- Agent directories: `agents/blog-writer/`, `agents/linkedin-writer/`.
- 24/7 headless content worker execution.

### Model-Agnostic Dispatch Flow
**Status**: `Next`
**Why**: Current dispatch bundles are Claude Code–specific (CLAUDE.md). Multi-provider workers need provider-aware delivery without changing dispatch creation.

**Goals**
- Tri-file format: `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` auto-generated from canonical dispatch.
- Converter layer in dispatcher — provider detected from terminal profile, correct file served.
- No change to T0 dispatch authoring workflow.

---

## Exploring (Not Default / Lower Priority)

## 11) YOLO Execution Mode
**Status**: `Exploring`  
**Why**: Useful to test autonomous completion boundaries, but conflicts with governance-first defaults.

**Scope**
- Optional mode with reduced friction (for controlled experiments only).
- Explicitly logged in receipts and visible in dashboard.
- Never default; always opt-in.

**Current Priority**
- Low. Governance + human-in-the-loop remains the primary operating model.

---

## 12) Additional Model Integrations (e.g., Kimi)
**Status**: `Exploring`  
**Why**: Further validate model-agnostic orchestration design.

**Goals**
- Add provider adapters without changing governance core.
- Capture capability differences in a provider matrix.
- Validate session/usage/receipt compatibility end-to-end.

---

## 13) Rust Core Prototype (Selective)
**Status**: `Exploring`  
**Why**: Evaluate memory-safe/runtime-efficient implementation for critical paths.

**Goals**
- Prototype a Rust implementation for selected core components.
- Candidate scope: receipt append/replay, state reconciliation, schema validation.
- Keep Python/Bash as reference behavior during evaluation.

**Constraints**
- No full rewrite commitment in this phase.
- Governance contracts and receipt compatibility stay non-negotiable.

---

## 14) Provider-Launch Layer as a Standalone Package
**Status**: `Exploring`
**Why**: The hardened multi-provider launch layer (`provider_dispatch` + `provider_spawns/*` + `adapters/*` + provider config, ~50 files with a clean `DispatchResult` contract) is the most reusable, self-contained part of VNX. Could ship as a slim standalone project for those who want only the orchestrator + provider lanes.

**Scope**
- Sever coupling to `project_root`, `canonical_event`, the report/receipt contract, and `provider_constraints`.
- Positioning: carry the no-SDK/CLI-driven + constraint-enforcement story (the differentiator), not bare routing (commodity vs litellm/aisuite).
- Next step: a dependency-audit (import-graph) to scope the exact extraction effort.

---

## 15) Harness-vs-Harness Model Comparison (Kimi CLI vs OpenRouter-via-Claude-harness)
**Status**: `Exploring`
**Why**: The GLM-5.2 benchmark showed a flat runner systematically underselling a model the full Claude harness reveals as top-tier. Open question: when a model already ships its own agentic CLI (Kimi K2.7), does the Claude harness add the same lift? This tests "two harnesses" rather than "harness vs flat" — a different, clarifying comparison.

**Scope**
- Run Kimi K2 through the litellm→OpenRouter proxy + Claude harness, compare vs the native `kimi` CLI lane on the same tasks.
- Brushes `kimi-via-cli-only` (a second Kimi lane for cost-tracking consistency); benchmark-only measurement, operator-gated.
- Generalizes: the same proxy can drive the full OpenRouter catalog through one harness.

---

## 16) Defect-Recall Benchmark (gate-model calibration)
**Status**: `Exploring`
**Why**: The t1-t6 field-tests measure WORKER quality (how well a model produces). Review GATES need a different thing — DEFECT-RECALL (how reliably a model finds flaws in others' code). The worker composite does not capture it (a model can be a mediocre producer but an excellent critic — e.g. codex's low worker score vs its proven "always finds something" on PR-4/PR-9). Gate-model selection currently rests on a heuristic; this makes it empirical, the gate analog of the worker benchmark.

**Scope**
- Corpus of PRs/diffs with a known ground-truth defect set (planted across categories: security, correctness, ADR-007, rollback-safety, races). t5_01 planted-review is the seed; scale it to multiple PRs.
- Every model reviews every PR, with launch-retry so lane reliability does not skew recall.
- Score recall (caught / planted), precision (real findings / total — penalize false-positive noise), and a per-category breakdown (which model catches which defect class).
- Output: a gate-model matrix (defect-recall per model per category) that calibrates the PM-skill gate-panel composition, separate from the worker-routing matrix.

---

## Living Roadmap (tracks database)

The active feature roadmap is the tracks database, operated through the `vnx objective` CLI (tracks, horizons, plan gates, deliverables). The repo-root `ROADMAP.yaml` is a generic example of the machine-readable roadmap format; `FEATURE_PLAN.md` and `PR_QUEUE.md` are views generated from it by `scripts/build_feature_plan.py` / `scripts/build_pr_queue.py`. The document you are reading captures the architecture principles and wave history; the public per-release summary is [`ROADMAP.md`](../../ROADMAP.md).

**1.0.1 focus:** operational hardening and provider reliability. Key items include Kimi content-block regression suite, log-rotation bounds for the event-stream ring buffer, tmux submit reliability hardening, and completion of the unified dispatch envelope for the Claude subprocess lane. (Per-append hash-chain enforcement shipped in #840; self-learning loop reactivation shipped in #850.)

**1.1 focus:** architectural extensibility. Key items include the full unified dispatch envelope across all lanes (`VNX_UNIFIED_ENVELOPE`), full role-to-capability binding (MCP allowlist, per-role permission mode), and OpenRouter as a provider lane.

## Roadmap Guardrails

- Keep append-only receipt path as canonical audit foundation.
- Keep human approval gates as default behavior.
- Keep provider hooks optional, never mandatory for core orchestration.
- Prefer explicit contracts and deterministic recovery over hidden automation.

---

## Out of Scope (for now)

- Hosted SaaS control plane
- Enterprise RBAC/compliance suite
- Fully distributed orchestration across remote machines
- Rewriting core runtime in Rust/Go before current governance objectives are complete

---

## Contribution Call

If you are a Rust or Go engineer interested in governance tooling for multi-agent workflows, contributions are welcome, especially in:

- deterministic receipt contracts and replay tooling
- state reconciliation correctness and test strategy
- performance and safety hardening of core runtime paths
