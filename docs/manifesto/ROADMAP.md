# VNX Roadmap

> **This file is the architecture-principles + wave-history roadmap.** Live, per-feature status lives in the maintainer's tracks database (the `vnx objective` CLI); the repo-root [`ROADMAP.yaml`](../../ROADMAP.yaml) is a generic example of the machine-readable roadmap format, not the live plan. Some milestone stamps below are historical (written April–May 2026) and may lag the current tree; trust the README and the tracks database (`vnx horizon queue`) for what is actually shipped.

**Status**: Public roadmap, statuses checked against `main` on 2026-10-18  
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

Key deliverables: `scripts/hooks/stop_report_hook.sh`, `scripts/lib/report_assembler.py`, `scripts/lib/classifier_providers/haiku_provider.py`, `VNX_AUTO_REPORT` feature flag.

### F38: Dashboard Unified
**Status**: `Completed` — April 2026

Single dashboard for coding and business domains. Domain filter tabs, session history browser, agent selector by name, reports browser surface.

Key deliverables: unified dashboard UI, domain tabs, session history, agent name selector.

### F39: Headless T0 Benchmark
**Status**: `Completed` — April 2026

Decision framework rewrite + gate locks + replay harness. A deterministic pre-filter runs in the replay harness before any LLM call. The benchmark scores of 2026-04 are not restated here: they were measured on recorded scenarios and have not been re-measured on `main`.

Key deliverables: `scripts/f39/replay_harness/` (including `prefilter.py`), `scripts/f39/gate_locks.py` (no production caller), `scripts/lib/context_assembler.py`. Taxonomy simplified to DISPATCH/COMPLETE/WAIT/REJECT/ESCALATE.

---

## Wave History

Wave-based planning tracks major capability milestones across the 2026 roadmap. All waves below are shipped and merged.

### Wave 1 — Shadow Read Cutover
**Status**: `Completed` — April 2026

Central VNX state shadow-mode validation. `scripts/lib/shadow_verifier.py` computes zero-tolerance divergence metrics (wrong-project rows, scoping/blocking-finding mismatch, top-3 divergence, count drift, lease-key collisions, p95-latency ratio). NDJSON divergence records under flock with timestamp-suffix rotation. All production T0 + IntelligenceSelector read sites wired through `VNX_USE_CENTRAL_DB` flag.

### Wave 2 — Package Extraction Foundation
**Status**: `Completed` — April 2026

`pyproject.toml` + `vnx_cli` package skeleton with smoke tests. Pipx-installable wheel foundation (`vnx` console_script entry-point). The package layout is now `vnx_cli` and `vnx_orchestration` (`pyproject.toml`); `function_size_gate.py` lives in `scripts/lib/`.

### Wave 4 / 4.5 / 4.6 — Provider Generalization
**Status**: `Completed` — April/May 2026

OTel observability foundation (opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`). `PromptAssembler` provider-agnostic methods for Claude/Codex/Gemini/LiteLLM. `provider_dispatch.py` entry-point with per-provider spawn handlers (`scripts/lib/provider_spawns/`). `CanonicalEvent` unified event shape via `EventStore` enforcement.

### Wave 5 — Control Centre + Multi-Project
**Status**: `Completed` — SHIPPED 2026-05-16

Single supervisor session managing N per-project T0 orchestrators. Multi-project state aggregator, per-project T0 lifecycle (spawn/heartbeat/kill/reap), multi-tenant lease isolation (schema v12), cross-project intelligence aggregator, hybrid dispatch routing, operator demo runbook.

Key deliverables: `docs/operations/CONTROL_CENTRE.md`, ADR-017.

### Wave 6 — Workers=N Elastic Pool
**Status**: `Completed` — SHIPPED 2026-05-16

Configurable worker pool with queue-aware + cost-aware scaling policies, dead-worker reap, and `vnx pool` CLI. PoolManager core (schema v14), pluggable scaling policies (`queue_depth_v1` + `cost_aware_v1`), provider-mix per pool, health monitoring + dead-worker reap tick cycle.

Key deliverables: `scripts/lib/pool_manager.py`, `.vnx/vnx_workers.default.yaml`, ADR-018.

### Wave 7 — Multi-Provider via LiteLLM
**Status**: `Completed` — SHIPPED 2026-05-17

Five providers in production: Claude, Codex CLI, Gemini CLI, Kimi CLI (OAuth), LiteLLM bridge (DeepSeek, GLM via OpenRouter). Uniform receipt + report shape across the providers. Intelligence injection, token + cost tracking, and quality gates equal first-class for every provider. Reproducible benchmark suite with routing recommendations (`scripts/benchmark/`).

This is the historical shipped-state at the time of Wave 7. It has since moved: Gemini CLI is a worker lane only, no longer a review gate (`RETIRED_GATE_NAMES`, operator decision 2026-09-26); the GLM allowlist is `glm-5.2`/`glm-5.3`/`glm-5.3-flash` (`glm-5.2` still the default); `deepseek_gate` defaults to `deepseek-flash` (#2025, released in v1.7.1). The `vnx providers` switch for provider families is enforced in every launch function (`docs/core/PROVIDER_SWITCH.md`, ADR-044).

Key deliverables: `scripts/lib/provider_spawns/litellm_spawn.py`, `scripts/lib/providers/routing_recommendations.yaml`, ADR-015.

### Wave 8 — Smart Router + Schema Enforcement
**Status**: `Completed` — SHIPPED 2026-05-17

Task-class-aware model routing (`smart_router.py`), hard provider constraint enforcement (`HardConstraintViolation` on policy breach), YAML frontmatter guardrails for uniform report schema, weekly drift + monthly benchmark cadence.

Key deliverables: `scripts/lib/smart_router.py`, `scripts/lib/constraint_enforcer.py`.

### 1.0 Sprint — Roadmap Autopilot, N-Scaling Foundation, Auto-Dream
**Status**: `Completed` — SHIPPED 2026-05-29

The PRs that completed the 1.0 capability surface. Major additions:

**Roadmap Autopilot (RA-1..6 + RA-3b)**
Gate-enforced autopilot primitives for the roadmap advance flow. RA-1 stamps `project_id` on `roadmap_state.json` and receipts (ADR-007 compliance). RA-2 provisions branch + worktree on `load_feature`. RA-3 enforces review-gate evidence in reconcile/advance. RA-3b closes 4 advance-gate bypass holes. RA-4 adds a human-approval gate primitive. RA-5 adds a `step` subcommand driving the active feature PR queue. RA-6 wires `autopilot_tick` + scheduler — ships dark (default off), gated by `VNX_ROADMAP_AUTOPILOT=1`.

**N-Scaling Foundation (N-1/2/3)**
N-1: atomic `claim_next_queued_dispatch` + migration 0026 for race-free queue consumption. N-2: `pool_worker_runner` single-claim entrypoint. N-3: `VNX_POOL_TASK_CONSUMER` wiring from pool worker spawn. Full pool-task-consumer path is opt-in; single-worker default unchanged.

**Auto-Dream Self-Learning Loop (runnable, not fully-active)**
ADR-019 consolidator core (`scripts/dream/`): quality DB consolidation, CLI + scheduler, GAP-7 receipt preflight, T0 review-gate, kimi timeout guard, canonical vnx_paths I/O routing. At the time of the sprint the loop was runnable on-demand and not routinely active. On main the dream consolidation and the nightly conversation analyzer honor the provider switch, and the dream consolidation reads `content_class` (#2057). Do not claim the loop as "active" without checking the scheduler on the machine in question.

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

Key deliverables: `autopilot_tick` in `scripts/roadmap_manager.py`, `scripts/lib/pool_worker_runner.py`, `scripts/dream/`, the `GOV-1` hook, `tracks` table + DAL, migration 0026.

---

## Milestones

Releases after 1.0.0 are listed in [`CHANGELOG.md`](../../CHANGELOG.md) and tagged in git. `VERSION` is the last release. A change that is only on `main` is "on main, not yet released".

### 1.0.0 Public Release
**Status**: `Released` — 2026-07-02. Published to PyPI (`pip install vnx-orchestration`); source tagged `v1.0.0`.

pip-installable, 5-provider production, governance receipts, elastic pool, smart routing (opt-in), roadmap-autopilot gate hardening (RA-1..5 active, RA-6 dark), auto-dream self-learning loop runnable. See [1.0 Sprint section](#10-sprint--roadmap-autopilot-n-scaling-foundation-auto-dream) for full capability list.

### Headless T0 Production
**Status**: `Planned`

Cutover from interactive T0 to autonomous headless T0 for standard feature chains. Not done: on main the T0 is an interactive session, guarded by `vnx guard` and rotated by `scripts/t0_rotate_spawn.sh`. Requires benchmark scores above 85% at Level-3, plus 3-Layer Trigger System operational, plus RA-6 autopilot-tick promoted from dark.

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
**Status**: Data centralization `Completed`; the per-project store model is decided in ADR-026.
**Why**: The multi-tenant central corpus is validated. Governance history of every project lives in a `project_id`-stamped store, and the live runtime writes to a per-project central dir resolved by `resolve_central_data_dir`. The shared corpus is the federation and analytics layer ([ADR-026](../governance/decisions/ADR-026-per-project-store-with-governance-federation.md)).

ADR-007 composite-key compliance is enforced: every central table keys over `project_id`. Per-project row counts and runtime-cutover status per consumer project change continuously and are not kept in this document.

---

## 4-Gate Enforcement Framework
**Status**: `Next`
**Why**: The review-gate stack that this item builds on has since changed: the default stack is `codex_gate,kimi_gate` (both subscription reviewers). When one is out, the takeover chain `codex_gate,kimi_gate,deepseek_gate` reads its seat, with each seat filled by its own reviewer. `glm_gate` left the default chain on 2026-10-17 (#2100). Gemini is no longer a review gate. Extending to a 4th deterministic gate with shift-left enforcement remains open.

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
**Why**: Next architectural milestone after centralization — standalone dispatcher replacing the shell-script dispatch loop.

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

## Governance Hardening Chain (first part shipped)
**Status**: Partly `Completed`
**Why**: The first four-feature autonomous run proved substantive delivery, but repeated governance gaps showed that formal closure and dispatch integrity needed a focused hardening lane.

Shipped on main: deterministic headless gate evidence enforcement (request, result and report must all exist, `docs/core/DISPATCH_RULES.md` §2 and `docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`) and the terminal input-ready mode guard (item 10 below). Later hardening is tracked in the tracks database, not here.

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
**Status**: `Completed` — receipt v2 (ADR-035, Accepted 2026-07-22)
**Why**: Contract evolution needed explicit compatibility guarantees.

Receipts carry `schema_version: 2` with `verdict{}`, `verification{}`, and `warnings[]` with an enforced destination rule; a pull-model query interface (`receipt_query.py`) replaced push delivery. See ADR-035 for the full field surface and its known follow-ups.

---

## 9) Refactoring and Simplification Sweep
**Status**: `Next`  
**Why**: Long-term reliability requires reducing complexity as features grow.

**Goals**
- Continue splitting large scripts into testable modules.
- Remove leftover legacy wrappers and dead paths where safe.
- Keep CLI behavior stable while improving maintainability.

A function-size ratchet runs in CI (`scripts/lib/function_size_gate.py`, #2036: on main, not yet released).

## 10) Terminal Input-Ready Mode Guard
**Status**: `Completed`
**Why**: Mouse-enabled tmux environments can leave a pane in copy/search mode, and slash-prefixed dispatches can then be swallowed by tmux itself.

`scripts/lib/input_mode_guard.sh` detects `pane_in_mode` before dispatch and fails closed when input readiness cannot be proven. Contract: `docs/core/110_INPUT_READY_TERMINAL_CONTRACT.md` (Status: Canonical).

---

## Next (After Committed Scope — Governance Hardening Series)

### Gate Locks v2
**Status**: `Next`
**Why**: A gate-locks primitive exists (`scripts/f39/gate_locks.py`, part of the F39 benchmark harness) but has no production caller — it does not cover any live review gate today. Building a locking layer that does cover CI green status, business compliance gates, and PR approval state is still open.

**Goals**
- Lock source: pull gate status from GitHub API / CI webhook, not manual file writes.
- Compound lock support: require multiple gates cleared before COMPLETE is allowed.
- Lock expiry: time-bounded locks for gates that need periodic re-verification.

### 3-Layer Trigger System
**Status**: `Completed`: `scripts/headless_trigger.py` (F41)
**Why**: A headless T0 needs event-driven wakeup with silent periods handled safely.

**Design as built**
- Layer 1: File watcher on `unified_reports/`, immediate trigger on new report arrival (`ReportWatcher`).
- Layer 2: Silence watchdog, every 10 min, deterministic checks for stale leases and stuck dispatches (`silence_watchdog`).
- Layer 3: LLM triage, haiku invoked only when an anomaly is detected, opt-in via `VNX_HAIKU_CLASSIFY` (`llm_triage`).

It wakes a headless T0 only. An interactive T0 is not woken by it (`docs/manifesto/GOVERNANCE_ARCHITECTURE.md` §5).

### F40: Business Agent Integration
**Status**: `Next`
**Why**: Replace fragile n8n → SSH → MacBook → claude -p pipeline for VNX Digital workers.

**On main**: agent directories exist under `agents/` (for example `agents/blog-writer/`). The goals below are not on main.

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
**Status**: `Completed` for Kimi, DeepSeek and GLM, shipped via Wave 7 (provider lanes in `docs/core/PROVIDER_LANES.md`). New providers remain an open-ended exploration.
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
**Why**: The hardened multi-provider launch layer (`provider_dispatch` + `provider_spawns/*` + `adapters/*` + provider config, with a clean `DispatchResult` contract) is the most reusable, self-contained part of VNX. Could ship as a slim standalone project for those who want only the orchestrator + provider lanes.

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

The active feature roadmap is the tracks database, operated through the `vnx horizon` CLI (alias `vnx objective`): tracks, horizons, plan gates and deliverables. `vnx horizon queue` is the read-only queue view over it. The repo-root `ROADMAP.yaml` is a generic example of the machine-readable roadmap format, not a source. No plan file is generated: `FEATURE_PLAN.md`, `PR_QUEUE.md` and the root `ROADMAP.md` were retired as inputs by ADR-047. The document you are reading captures the architecture principles and wave history.

Review seats, the takeover chain, the plan-gate with one seat, open outcomes and the merge door are summarised in `docs/manifesto/GOVERNANCE_ARCHITECTURE.md` §10. The per-milestone forward plan (what ships in the next point release) lives in the tracks database, not in this document — it moves too often to keep in sync here. Check `vnx objective` for the current state.

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
