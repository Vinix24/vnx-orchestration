# VNX Roadmap

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

## Milestones

### Headless T0 Production
**Status**: `Planned` — Target: Q2 2026

Cutover from interactive T0 to autonomous headless T0 for standard feature chains. Requires benchmark scores above 85% at Level-3, plus 3-Layer Trigger System operational.

Success criteria: T0 makes correct dispatch/complete/wait decisions autonomously across 10 consecutive real feature chains without operator override.

---

## Committed (Near Term)

## 1) Multi-Feature PR Queue
**Status**: `Committed`  
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
**Status**: `Committed`  
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
**Status**: `Committed`  
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
**Status**: `Committed`  
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
**Status**: `Next`  
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
