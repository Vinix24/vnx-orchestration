# Internal Plans Project Status

**Status**: Active (Internal)
**Last Updated**: 2026-04-03
**Owner**: T0 / Planning
**Purpose**: Internal program-level status for the autonomous coding track. This document tracks confirmed baseline capability, authored planning progress, and the recommended next feature order.

---

## 1. Current Snapshot

Features 12, 13, 14, 15, 16, and 17 are complete. Feature 17 merged on `feature/rich-headless-runtime-sessions-and-structured-observability` branch, 2026-04-03.

The internal baseline is now:

- runtime truth is explicit and governable (Feature 12)
- operator dashboard surfaces runtime, session, and open-item truth through a governed read model (Feature 13)
- multi-feature chain execution is governed with deterministic advancement, recovery, and carry-forward (Feature 14)
- context injection is bounded and measurable, handovers are structured and validated (Feature 15)
- runtime adapter boundary is explicit: TmuxAdapter formalized, HeadlessAdapter skeleton operational, RuntimeFacade routes all calls (Feature 16)
- headless runtime sessions are first-class: LocalSessionAdapter with lifecycle, structured event stream, and provider-aware observability (Feature 17)
- all six features certified with 1080+ tests, zero blocker open items
- headless review gate infrastructure corrected: both Gemini and Codex now default-enabled with atomic request-and-execute flow
- next emphasis: secured five-feature chain pilot (Features 18–22) with checkpoints and post-run analysis; Gemini gates disabled for this pilot due to rate limits, Codex required

---

## 2. Confirmed Capability Baseline

The confirmed autonomous coding hardening baseline now includes:

**Pre-Feature 12 (Features 5–11)**:
- terminal input-readiness protection
- queue/runtime projection consistency hardening
- gate-evidence accuracy and PR-scoped lookup discipline
- dispatch requeue and classification accuracy
- delivery substep observability

**Feature 12 — Runtime State Machine And Stall Supervision**:
- 9-state canonical worker lifecycle (initializing → working → stalled → exited_clean/exited_bad/resume_unsafe)
- heartbeat and output tracked as independent liveness signals
- stall detection with automatic open-item escalation (9 anomaly types)
- deterministic truth hierarchy: Runtime DB > Queue Projection > Terminal Activity
- zombie lease and ghost dispatch tie-break detection

**Feature 13 — Coding Operator Dashboard And Session Control**:
- 5 operator surfaces with 30+ mapped operator questions
- read-model layer with FreshnessEnvelope (fresh/aging/stale classification)
- 6 safe operator actions with structured ActionOutcome model
- Next.js operator control surface with SWR auto-refresh
- cross-project open-item aggregation with attention model
- forbidden data path enforcement (UI never reads raw files)

**Gate infrastructure hardening (post-Feature 13)**:
- Codex gate default changed to enabled
- atomic `request-and-execute` command prevents request-without-execute
- stall thresholds calibrated for agentic CLI behavior (180s Gemini, 300s Codex)

**Feature 14 — Multi-Feature Autonomy Hardening And Chain Recovery**:
- 8-state chain execution model (INITIALIZED through CHAIN_COMPLETE/HALTED)
- deterministic advancement: merged PR + certified gates + no blocker items required
- recovery decision tree: 3 failure classes with retry limits (2 per class, 3 total)
- resume-safe vs resume-unsafe chain state classification
- branch baseline guard with merge-base + is-ancestor validation
- carry-forward ledger: cumulative findings, open items, deferred items, residual risks
- deferred item validation: blocker deferral rejected at code level (O-3 enforcement)
- chain residual governance model for multi-feature runs
- 130 chain-specific tests across 4 test files

**Feature 15 — Context Injection And Handover Quality**:
- bounded 7-priority context component model (P0-P7) with explicit budget enforcement
- context overhead < 20% target, 25% hard limit, with reverse-priority trimming
- stale-context rejection with per-component max age (0s for chain state, 24h for intelligence, 14d for signals)
- structured handover payload with 5 invariants (HO-1..HO-5) and validation
- resume payload supporting rotation, interruption, and redispatch with 5 invariants (RS-1..RS-5)
- reusable outcome signal extraction from receipts, open items, and carry-forward ledger
- 180 context/handover tests across 4 test files

**Feature 16 — Runtime Adapter Formalization And Headless Transport Abstraction**:
- RuntimeAdapter protocol with 12 operations and 9 named capabilities
- TmuxAdapter: formalized implementation supporting all 9 capabilities with feature flag preservation
- HeadlessAdapter: subprocess-based skeleton supporting 7 capabilities (ATTACH and REHEAL explicitly unsupported)
- RuntimeFacade: unified routing layer with capability pre-check before every operation
- Canonical state isolation: adapter never writes lease/dispatch state, only records coordination events
- Direct coupling freeze: test-enforced ban on new subprocess+tmux calls outside adapter files
- 3 pre-existing direct tmux violations cataloged (dashboard_actions.py, terminal_snapshot.py, terminal_state_reconciler.py) — not introduced by Feature 16
- 29 certification tests, 125 adapter tests, 242 supporting runtime tests (396 total)

**Feature 17 — Rich Headless Runtime Sessions And Structured Observability**:
- Headless session contract with session/attempt/run identity model and S-1..S-5 invariants
- LocalSessionAdapter: explicit lifecycle (CREATED->RUNNING->COMPLETED|FAILED|TIMED_OUT) with attempt tracking
- HeadlessEventStream: 7 structured event types, NDJSON serialization, canonical order validation, artifact correlation
- Provider-aware observability: 4 providers with capability flags (tool_call_visibility, structured_progress_events, output_only_fallback, can_attach)
- ObservabilityQuality projections: RICH (claude_code), STRUCTURED (gemini), OUTPUT_ONLY (codex_cli, unknown)
- Progress confidence derived from provider capabilities: high/medium/low
- Unknown providers degrade to output-only explicitly
- 36 certification tests, 95 component tests (131 total Feature 17)

The system is now:
- governance-first with explicit runtime truth
- receipt-led with operator dashboard visibility
- headless-review capable with both Gemini and Codex enabled by default
- materially hardened against silent runtime failure modes
- chain-capable: multi-feature execution governed with deterministic recovery and cumulative carry-forward
- context-bounded: dispatch prompts have measurable overhead, structured handovers, and validated resumes
- adapter-bounded: runtime behavior flows through explicit adapter boundary with capability gating and coupling freeze
- session-aware: headless execution modeled as governed sessions with structured observability and provider-honest visibility

---

## 3. Newly Added Planning Milestones

### Agent OS internal stack

Added internal planning artifacts:

- [AGENT_OS_STRATEGY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/strategy/AGENT_OS_STRATEGY.md)
- [AGENT_OS_REQUIREMENTS_AND_GUARDRAILS.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/contracts/AGENT_OS_REQUIREMENTS_AND_GUARDRAILS.md)
- [AUTONOMOUS_CODING_UPGRADE_ROADMAP.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/roadmap/AUTONOMOUS_CODING_UPGRADE_ROADMAP.md)

### Next executable feature plans

Added detailed execution plans:

- [FEATURE_PLAN_AUTONOMOUS_RUNTIME_STATE_MACHINE_AND_STALL_SUPERVISION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_AUTONOMOUS_RUNTIME_STATE_MACHINE_AND_STALL_SUPERVISION.md)
- [FEATURE_PLAN_CODING_OPERATOR_DASHBOARD_AND_SESSION_CONTROL.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CODING_OPERATOR_DASHBOARD_AND_SESSION_CONTROL.md)
- [FEATURE_PLAN_MULTI_FEATURE_AUTONOMY_HARDENING_AND_CHAIN_RECOVERY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_MULTI_FEATURE_AUTONOMY_HARDENING_AND_CHAIN_RECOVERY.md)
- [FEATURE_PLAN_CONTEXT_INJECTION_AND_HANDOVER_QUALITY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CONTEXT_INJECTION_AND_HANDOVER_QUALITY.md)
- [FEATURE_PLAN_RUNTIME_ADAPTER_FORMALIZATION_AND_HEADLESS_TRANSPORT_ABSTRACTION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_RUNTIME_ADAPTER_FORMALIZATION_AND_HEADLESS_TRANSPORT_ABSTRACTION.md)
- [FEATURE_PLAN_RICH_HEADLESS_RUNTIME_SESSIONS_AND_STRUCTURED_OBSERVABILITY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_RICH_HEADLESS_RUNTIME_SESSIONS_AND_STRUCTURED_OBSERVABILITY.md)
- [FEATURE_PLAN_LEARNING_LOOP_SIGNAL_ENRICHMENT_AND_GOVERNANCE_FEEDBACK_HARDENING.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_LEARNING_LOOP_SIGNAL_ENRICHMENT_AND_GOVERNANCE_FEEDBACK_HARDENING.md)
- [FEATURE_PLAN_CODING_SUBSTRATE_GENERALIZATION_AND_AGENT_OS_LIFT_IN.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CODING_SUBSTRATE_GENERALIZATION_AND_AGENT_OS_LIFT_IN.md)
- [FEATURE_PLAN_BUSINESS_LIGHT_GOVERNANCE_PILOT_AND_FOLDER_SCOPED_ORCHESTRATION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_BUSINESS_LIGHT_GOVERNANCE_PILOT_AND_FOLDER_SCOPED_ORCHESTRATION.md)
- [FEATURE_PLAN_REGULATED_STRICT_GOVERNANCE_PROFILE_AND_AUDIT_BUNDLE.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_REGULATED_STRICT_GOVERNANCE_PROFILE_AND_AUDIT_BUNDLE.md)
- [FEATURE_PLAN_PREFERENCES_AND_LESSONS_SURFACE_GENERALIZATION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_PREFERENCES_AND_LESSONS_SURFACE_GENERALIZATION.md)
- [CHAIN_PILOT_FEATURES_18_22.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/CHAIN_PILOT_FEATURES_18_22.md)

---

## 4. What Feature 12 And 13 Proved

### Feature 12 — Proven

Runtime truth is now explicit and governable:
- stalled vs exited vs stale vs active is distinguishable under test (129 tests)
- silent bad runtime states are structurally impossible (heartbeat_dead_threshold + stall_threshold = 480s max)
- anomalies become durable evidence and auto-escalated open items
- T0 has deterministic truth hierarchy — no more guessing which surface to trust

### Feature 13 — Proven

The operator can act on runtime truth:
- start/stop sessions per project from dashboard
- see terminal health with heartbeat classification
- inspect per-project and aggregate open items with severity sorting
- degraded state is unmissable (red banner, never silent)
- UI enforces read-model contract — zero direct file access

### Feature 14 — Proven

Multi-feature chain execution is now governed instead of ad hoc:
- chain state model with 8 explicit states and deterministic transitions — no implicit state drift
- advancement requires triple-gate truth: PR merged + review gates certified + no blocker open items
- recoverable interruptions requeue deterministically (max 2 per failure class, 3 total) — no infinite loops, no manual re-orchestration
- carry-forward ledger accumulates findings, items, and risks across feature boundaries — nothing silently dropped
- branch baseline guard prevents stale worktree drift via merge-base + is-ancestor check
- deferred item validation enforces contract O-3 at code level — blocker deferral structurally impossible
- full 5-feature lifecycle with one recovery cycle proven under test (130 tests)

### Feature 15 — Proven

Context injection and handover quality are now bounded and measurable:
- 7-priority context component model with explicit budget enforcement — overhead must stay under 20%/25%
- stale-context rejection with per-component max age — chain state (0s), intelligence (24h), signals (14d)
- structured handover payloads with 5 invariants and validation — no more ad-hoc markdown
- resume payloads for 3 scenarios (rotation, interruption, redispatch) with specificity enforcement
- reusable outcome signals extracted from receipts, open items, and carry-forward — P7 context feed
- transcript detection (RS-5) prevents raw conversation history from entering resume payloads
- 180 tests certifying budget enforcement, handover completeness, resume acceptance, and stale rejection

### Gate evidence caveat

Feature 12/13 were merged with interactive review only. Headless gates were not executed during the chain due to:
1. `gate_runner.py` absent at Feature 12 branch-cut (infrastructure timing gap)
2. T0 orchestration allowed request-without-execute (flow gap)

Correction applied post-chain: both providers now default-enabled, atomic execution enforced.
Retroactive gate execution in progress for certification PRs.

---

## 5. Recommended Next Feature Order

Features 12, 13, 14, 15, and 16 are complete. Default order from this point:

1. ~~Feature 12: runtime state machine and stall supervision~~ — **COMPLETE**
2. ~~Feature 13: coding operator dashboard and session control~~ — **COMPLETE**
3. ~~Feature 14: multi-feature autonomy hardening~~ — **COMPLETE**
4. ~~Feature 15: context injection and handover quality~~ — **COMPLETE**
5. ~~Feature 16: runtime adapter formalization and headless transport abstraction~~ — **COMPLETE**
6. ~~Feature 17: rich headless runtime sessions and structured observability~~ — **COMPLETE**
7. Feature 18: learning-loop signal enrichment and governance feedback hardening
8. Feature 19: coding substrate generalization and Agent OS lift-in
9. Feature 20: business-light governance pilot and folder-scoped orchestration
10. Feature 21: regulated-strict governance profile and audit bundle
11. Feature 22: preferences and lessons surface generalization

Prerequisite for Feature 18: Feature 17 merged with green CI and both review gates passing.

Order rule:

- no later feature should begin until the previous feature’s certification PR is merged from green CI
- exceptions are only allowed when the dependency edge is explicitly narrow and T0 documents the reason

---

## 6. Advancement Gates

Before the program advances from one feature to the next, all of the following must be true:

- final certification PR merged by human decision
- required GitHub CI green
- Gemini review blocking findings closed
- Codex gate blocking findings closed where required
- chain-created open items resolved or explicitly carried forward
- this file updated
- [CHANGELOG.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/CHANGELOG.md) updated

---

## 7. Maintenance Rule

Every future feature certification PR must update both:

- [CHANGELOG.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/CHANGELOG.md)
- [PROJECT_STATUS.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/PROJECT_STATUS.md)

Required minimum closeout content:

- what was completed
- what capability is now materially better or newly proven
- what remains risky or deferred
- what the next recommended feature order is
