# Internal Plans Changelog

**Status**: Active (Internal)
**Last Updated**: 2026-04-04
**Owner**: T0 / Planning
**Purpose**: Running changelog for the internal autonomous-coding program, tracking planning milestones, execution-progress milestones, and the ordered next steps expected from future feature completion.

---

## 2026-04-04

### Feature 26 completed: Terminal Startup And Session Control

Merged: PRs on `fix/serve-dashboard-module-split` branch, 2026-04-04

Material changes:
- profile-aware start_session(): coding_strict (dev) projects receive 2x2 tmux layout (4 panes), business_light projects receive single terminal
- profile auto-detection via governance_profile_selector with fallback to vnx start when detection fails
- session stop via vnx stop with clean tmux teardown
- dry-run mode returns planned layout and session state without executing any tmux or subprocess calls
- dashboard session control buttons: Start, Stop, Attach with pending states, cross-button disable, and structured outcome display
- serve_dashboard.py module split: api_operator.py (762 lines) and api_token_stats.py (380 lines) extracted, reducing serve_dashboard.py from ~1570 to 438 lines
- 28 session profile tests + 33 dashboard actions tests + 62 dashboard feature tests + 60 serve dashboard API tests + 25 frontend session control tests = 208 total Feature 26 tests

Resolves:
- OI-373: dashboard_actions.py:start_session refactored — no longer a monolithic function, profile-aware with direct tmux path
- OI-374: serve_dashboard.py decomposed — 3 focused modules (438 + 762 + 380 lines)

Artifacts:
- `scripts/lib/dashboard_actions.py` — profile-aware start_session, stop_session, attach_terminal (extended)
- `dashboard/api_operator.py` — extracted operator API handlers (762 lines)
- `dashboard/api_token_stats.py` — extracted token stats handlers (380 lines)
- `dashboard/serve_dashboard.py` — slimmed core server (438 lines)
- `dashboard/token-dashboard/components/operator/project-card.tsx` — session control UI (extended)
- `tests/test_session_start_profile.py` — 28 profile and layout tests
- `dashboard/token-dashboard/__tests__/session-control-buttons.test.tsx` — 25 UI tests

### Feature 25 completed: Governance Digest Pipeline And Dashboard Surface

Merged: PRs on `feat/pr2-digest-api-endpoint-and-signal` branch, 2026-04-04

Material changes:
- governance digest pipeline contract (D-1..D-5 invariants) defining 5-min daemon cadence, digest JSON schema, freshness tracking, advisory-only enforcement
- GovernanceDigestRunner in intelligence_daemon.py: reads gate results and queue anomalies from t0_receipts.ndjson, delegates to F18 extractors (collect_governance_signals + build_digest), writes governance_digest.json atomically
- SignalStore: append-only NDJSON store with thread-safe writes, atomic append, from_env factory
- GET /api/operator/governance-digest: freshness envelope with staleness tracking, degraded state detection (missing/stale file)
- governance dashboard page (S7): KPI strip, recurrence table with severity badges, recommendation cards with advisory-only badge, signal volume chart (recharts)
- TypeScript types: GovernanceDigestEnvelope, DigestRecurrenceRecord, DigestRecommendation, GovernanceDigestData
- SWR hook (useGovernanceDigest) with 60s refresh interval
- 48 certification tests + 91 Python component tests + 39 JS component tests = 178 Feature 25 tests

Artifacts:
- `docs/GOVERNANCE_DIGEST_PIPELINE_CONTRACT.md` — digest pipeline contract (v1)
- `scripts/intelligence_daemon.py` — GovernanceDigestRunner (extended)
- `scripts/lib/signal_store.py` — NDJSON signal store (91 lines)
- `dashboard/serve_dashboard.py` — governance-digest API endpoint (extended)
- `dashboard/token-dashboard/app/operator/governance/page.tsx` — governance page (822 lines)
- `dashboard/token-dashboard/lib/types.ts` — TypeScript types (extended)
- `tests/test_governance_digest_certification.py` — 48 certification tests

## 2026-04-03

### Feature 22 completed: Preferences And Lessons Surface Generalization

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- preferences/lessons contract defining entity model (preferences + lessons), scoping (profile/domain), cross-profile isolation (PL-1..PL-5), authority boundaries (PA-1..PA-5), and 90-day retirement
- preference store with immutable PreferenceEntry, ScopeKey-based query, profile validation, scope contamination guard
- preference injector with profile-scoped dispatch injection, bounded context, InjectionContext frozen dataclass
- preference surface with ProfileSurface snapshot, active/retired counting, format_surface_line operator view
- lesson conflict detection and resolution with ConflictPair, ResolutionKind (ACCEPT/DEFER/RETIRE), append-only ResolutionLog audit trail
- 20 certification tests + 215 component tests = 235 Feature 22 tests

Artifacts:
- `docs/PREFERENCES_LESSONS_CONTRACT.md` — preferences/lessons contract (v1)
- `scripts/lib/preference_store.py` — scoped preference/lesson store (359 lines)
- `scripts/lib/preference_injector.py` — profile-scoped injection (216 lines)
- `scripts/lib/preference_surface.py` — operator dashboard surface (196 lines)
- `scripts/lib/lesson_conflict.py` — conflict detection and resolution (406 lines)
- `tests/test_preferences_lessons_certification.py` — 20 certification tests

### Feature 24 completed: Per-Project Open Items And Gate Toggle

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- open items and gate toggle contract with project switcher UX, gate toggle API, safe-action A7
- gate config endpoints: GET /api/operator/gate/config, POST /api/operator/gate/toggle with YAML persistence
- open items page with project dropdown and severity filter chips
- gate toggle switches on project cards
- 10 certification tests + 40 component tests = 50 Feature 24 tests

---

### Feature 23 completed: Dashboard Data Pipeline Fix And Kanban Board

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- dashboard kanban contract with S6 surface (5 columns), dispatch-to-stage mapping, health endpoint, error/degraded rendering
- health endpoint GET /api/health with 5 data source checks, uptime tracking, all_sources_available flag
- kanban dispatch scanning with directory-to-stage mapping, bundle parsing, receipt promotion
- kanban board frontend (Next.js): 5-column view with track colors, duration display, card details
- 15 certification tests + 153 component tests = 168 Feature 23 tests

Artifacts:
- `docs/DASHBOARD_KANBAN_CONTRACT.md` — kanban contract (v1)
- `dashboard/serve_dashboard.py` — health + kanban endpoints
- `dashboard/token-dashboard/app/operator/kanban/page.tsx` — kanban board component
- `tests/test_dashboard_kanban_certification.py` — 15 certification tests

---

### Chain pilot Features 18-22 complete

This closes the five-feature chain pilot (Features 18-22). All features certified with 2225+ tests. See `CHAIN_PILOT_18_22_REPORT.md` for full pilot analysis.

---

### Feature 21 completed: Regulated-Strict Governance Profile And Audit Bundle

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- regulated-strict governance contract with explicit approval workflow (7-state machine), approval records with RA-1..RA-4 invariants, audit bundle with 5 evidence types and completeness gating
- approval workflow: DispatchApprovalState tracking pre-execution and post-review approvals, EmptyRationaleError, AutomatedApprovalError enforcement
- audit bundle builder: immutable AuditBundle and EvidenceEntry with AB-1..AB-5 invariants, dispatch_id cross-dispatch isolation guards, MappingProxyType payload protection
- regulated-strict dashboard: RegulatedStrictStatus frozen snapshot, profile-locked invariant, format_status_line operator surface
- 21 certification tests + 255 component tests = 276 Feature 21 tests

Artifacts:
- `docs/REGULATED_STRICT_GOVERNANCE_CONTRACT.md` — regulated-strict contract (v1)
- `scripts/lib/regulated_strict_approval.py` — approval workflow (612 lines)
- `scripts/lib/audit_bundle.py` — audit bundle builder (479 lines)
- `scripts/lib/regulated_strict_dashboard.py` — dashboard surface (227 lines)
- `tests/test_regulated_strict_certification.py` — 21 certification tests

---

### Feature 20 completed: Business-Light Governance Pilot And Folder-Scoped Orchestration

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- business-light governance contract defining review-by-exception policy, folder-scoped orchestration, cross-profile authority isolation, and pilot limits with rollback criteria
- folder scope module with ScopeType enum, FolderScope/FolderContext frozen dataclasses, IsolationViolation enforcement, and scope resolution without filesystem I/O
- business-light review policy with OpenItem severity-based blocking, AuditRecord immutable artifact trail, CloseoutDecision explicit invariant, and GateResult evaluation
- governance profile selector: coding scopes always get CODING_STRICT (cannot be overridden), business scopes may request BUSINESS_LIGHT, ProfileVisibility operator surface
- 30 certification tests + 221 component tests = 251 Feature 20 tests

Artifacts:
- `docs/BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` — business-light governance contract (v1)
- `scripts/lib/folder_scope.py` — folder-scoped orchestration (256 lines)
- `scripts/lib/business_light_policy.py` — review-by-exception policy (263 lines)
- `scripts/lib/governance_profile_selector.py` — profile selection and visibility (288 lines)
- `tests/test_business_light_certification.py` — 30 certification tests

---

### Feature 19 completed: Coding Substrate Generalization And Agent OS Lift-In

Merged: PRs on `feature/orchestration-substrate-extraction` branch, 2026-04-03

Material changes:
- Agent OS lift-in contract defining 3-layer model (Transport, Substrate, Domain), 4 boundary invariants (B-1..B-4), 5 governance invariants (G-1..G-5), and 6 anti-goals
- orchestration substrate with domain-agnostic StateTransitionSpec, WorkerHandle, ManagerProtocol, and CodingManagerAdapter compatibility bridge
- capability profile model with 20 capability constants, MaturityLevel enum, DomainReadinessSurface, and honest readiness projections
- domain plan scaffolding template with onboarding section, capability profile declaration, and substrate boundary acknowledgments
- domain plan validator with 8 rules (V-1..V-8) blocking premature rollout, missing onboarding, and policy mutation
- import boundary verified: all substrate modules use stdlib-only imports (zero domain-specific imports)
- 22 certification tests + 130 component tests = 152 Feature 19 tests

Artifacts:
- `docs/AGENT_OS_LIFT_IN_CONTRACT.md` — Agent OS lift-in contract (v1)
- `scripts/lib/orchestration_substrate.py` — domain-agnostic orchestration substrate
- `scripts/lib/capability_profiles.py` — capability profiles and readiness surfaces
- `scripts/lib/domain_plan_validator.py` — plan scaffolding validator (8 rules)
- `templates/DOMAIN_FEATURE_PLAN_TEMPLATE.md` — domain plan template with guardrails
- `tests/test_agent_os_certification.py` — 22 certification tests

---

### Feature 18 completed: Learning-Loop Signal Enrichment And Governance Feedback Hardening

Merged: PRs on `main` branch, 2026-04-03

Material changes:
- governance feedback loop contract defining 7 signal classes, recurrence thresholds, authority boundary, and local-model helper role
- enriched governance signal extraction from 4 source categories (session events, gate results, queue anomalies, open-item transitions) with full correlation context
- defect family normalization via MD5 key derivation for deterministic recurrence matching
- recurrence detection with 2-occurrence threshold, 5-occurrence high-frequency escalation
- retrospective digest surface with evidence-linked recurrence patterns and guarded recommendations
- 4 recommendation categories (review_required, runtime_fix, policy_change, prompt_tuning) with enforced advisory_only invariant
- optional local-model retrospective analysis hook with non-authoritative constraint, confidence annotation, and fallback behavior
- 31 certification tests + 217 component tests = 248 Feature 18 tests

Artifacts:
- `docs/GOVERNANCE_FEEDBACK_CONTRACT.md` — governance feedback loop contract (v1)
- `scripts/lib/governance_signal_extractor.py` — signal enrichment (476 lines)
- `scripts/lib/retrospective_digest.py` — recurrence detection and digests (391 lines)
- `scripts/lib/retrospective_model_hook.py` — local-model hook (257 lines)
- `tests/test_learning_loop_certification.py` — 31 certification tests

---

### Feature 17 completed: Rich Headless Runtime Sessions And Structured Observability

Merged: PRs on `feature/rich-headless-runtime-sessions-and-structured-observability` branch, 2026-04-03

Material changes:
- canonical headless session contract extending HEADLESS_RUN_CONTRACT with session/attempt/run identity model, structured event schema, evidence classes, and provider-capability matrix
- LocalSessionAdapter with explicit lifecycle states (CREATED->RUNNING->COMPLETED|FAILED|TIMED_OUT) and monotonic attempt tracking
- HeadlessEventStream with 7 structured event types, NDJSON serialization, canonical order validation, and artifact correlation
- provider-aware observability layer: 4 providers (claude_code, gemini, codex_cli, output_only) with capability flags and quality projections (RICH, STRUCTURED, OUTPUT_ONLY)
- progress confidence derived from provider capabilities (high/medium/low)
- unknown provider fallback to output-only with explicit degradation
- RuntimeAdapter protocol conformance for LocalSessionAdapter (7 capabilities, ATTACH/REHEAL unsupported)
- 36 certification tests + 95 component tests = 131 Feature 17 tests

Codex findings resolved:
- OI-619: S-1 identity clarified with (terminal_id, dispatch_id, attempt_generation) key
- OI-620: confidence field added to run.progress schema
- OI-621: Evidence completeness aligned with artifacts/log_artifact.txt

Artifacts:
- `docs/HEADLESS_SESSION_CONTRACT.md` — headless session contract (v1)
- `scripts/lib/local_session_adapter.py` — LocalSessionAdapter (288 lines)
- `scripts/lib/headless_event_stream.py` — structured event stream (184 lines)
- `scripts/lib/provider_observability.py` — provider capabilities (141 lines)
- `tests/test_rich_headless_certification.py` — 36 certification tests
- `tests/test_local_session_adapter.py` — 28 lifecycle tests
- `tests/test_headless_event_stream.py` — 28 event stream tests
- `tests/test_provider_observability.py` — 34 provider tests

### Recommended next execution order

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

### Chain pilot gate exception

- Features 18–22 chain pilot runs with Gemini headless gates disabled due to rate limits; Codex gates remain required.

### Feature plans added for secured five-feature chain

- [FEATURE_PLAN_BUSINESS_LIGHT_GOVERNANCE_PILOT_AND_FOLDER_SCOPED_ORCHESTRATION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_BUSINESS_LIGHT_GOVERNANCE_PILOT_AND_FOLDER_SCOPED_ORCHESTRATION.md)
- [FEATURE_PLAN_REGULATED_STRICT_GOVERNANCE_PROFILE_AND_AUDIT_BUNDLE.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_REGULATED_STRICT_GOVERNANCE_PROFILE_AND_AUDIT_BUNDLE.md)
- [FEATURE_PLAN_PREFERENCES_AND_LESSONS_SURFACE_GENERALIZATION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_PREFERENCES_AND_LESSONS_SURFACE_GENERALIZATION.md)
- [CHAIN_PILOT_FEATURES_18_22.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/CHAIN_PILOT_FEATURES_18_22.md)

---

### Feature 16 completed: Runtime Adapter Formalization And Headless Transport Abstraction

Merged: PRs on `feature/multi-feature-autonomy-hardening` branch (continued), 2026-04-03

Material changes:
- canonical RuntimeAdapter protocol defining 12 operations (spawn, stop, deliver, attach, observe, inspect, health, session_health, reheal, adapter_type, capabilities, shutdown)
- adapter capability model with 9 named capabilities and explicit UnsupportedCapability semantics
- TmuxAdapter formalized as one implementation of RuntimeAdapter, supporting all 9 capabilities
- HeadlessAdapter skeleton implementing 7 capabilities (ATTACH and REHEAL explicitly unsupported)
- RuntimeFacade routing all orchestration/dashboard calls through adapter with capability pre-checks
- direct tmux coupling freeze guard: test-enforced ban on new subprocess+tmux calls outside adapter files
- canonical state mapping rules: adapter never writes lease/dispatch state, only records coordination events
- session_health() accepts caller-provided terminal_ids (adapter never queries canonical DB)
- deliver() uses dispatcher-provided lease_hint for metadata only (no adapter-side lease reads)
- 3 pre-existing direct tmux coupling violations identified (dashboard_actions.py, terminal_snapshot.py, terminal_state_reconciler.py) — predating Feature 16, cataloged for future cleanup
- 29 certification tests + 125 adapter tests + 242 supporting runtime tests = 396 total

Artifacts:
- `docs/RUNTIME_ADAPTER_CONTRACT.md` — canonical runtime adapter contract (v1)
- `scripts/lib/adapter_protocol.py` — RuntimeAdapter protocol and validation helpers
- `scripts/lib/headless_transport_adapter.py` — HeadlessAdapter skeleton (174 lines)
- `scripts/lib/runtime_facade.py` — RuntimeFacade routing layer (185 lines)
- `tests/test_runtime_adapter_certification.py` — 29 certification tests
- `tests/test_adapter_conformance.py` — 31 conformance tests
- `tests/test_tmux_adapter_interface.py` — 29 interface and freeze tests
- `tests/test_runtime_facade.py` — 25 facade tests

Codex findings resolved during Feature 16:
- OI-559: deliver() lease soft-check contradiction — replaced with lease_hint parameter
- OI-560: CAPTURE_OUTPUT/INTERACTIVE_INPUT without operations — moved to reserved capabilities
- OI-561: session_health() scope ambiguity — added terminal_ids parameter

### Recommended next execution order

1. ~~Feature 12: runtime state machine and stall supervision~~ — **COMPLETE**
2. ~~Feature 13: coding operator dashboard and session control~~ — **COMPLETE**
3. ~~Feature 14: multi-feature autonomy hardening~~ — **COMPLETE**
4. ~~Feature 15: context injection and handover quality~~ — **COMPLETE**
5. ~~Feature 16: runtime adapter formalization and headless transport abstraction~~ — **COMPLETE**
6. Feature 17: rich headless runtime sessions and structured observability
7. Feature 18: learning-loop signal enrichment and governance feedback hardening
8. Feature 19: coding substrate generalization and Agent OS lift-in

---

## 2026-04-02

### Feature 15 completed: Context Injection And Handover Quality

Merged: PRs on `feature/multi-feature-autonomy-hardening` branch (continued), 2026-04-02

Material changes:
- context injection contract defining bounded 7-priority component model (P0-P7)
- context assembler with budget enforcement: P3-P7 overhead < 20% target, 25% hard limit
- stale-context rejection with per-component max age and freshness metadata
- structured handover payload with HO-1..HO-5 invariants and validation
- resume payload supporting rotation, interruption, and redispatch with RS-1..RS-5 invariants
- reusable outcome signal extraction from receipts, open items, and carry-forward ledger
- deferred item validation at code level (blocker deferral rejected)
- 180 context/handover tests across 4 test files
- Codex findings resolved: Queue Truth alignment, budget model clarification, chain state source

Artifacts:
- `docs/CONTEXT_INJECTION_CONTRACT.md` — context injection and handover contract
- `scripts/lib/context_assembler.py` — bounded context assembly with budget enforcement
- `scripts/lib/handover_resume.py` — handover and resume payload generation with validation
- `scripts/lib/outcome_signals.py` — reusable outcome signal extraction (P7)
- `tests/test_context_assembler.py` — 55 context assembler tests
- `tests/test_handover_resume.py` — 53 handover/resume tests
- `tests/test_outcome_signals.py` — 36 outcome signal tests
- `tests/test_context_resume_certification.py` — 36 certification tests

Context rotation finding:
- 65% context rotation hooks are correctly wired but `vnx_rotate.sh` fails due to tmux session name mismatch (hardcoded `vnx` vs actual `vnx-vnx-roadmap-autopilot-wt`). Fix required in main vnx-system repo.

### Recommended next execution order

1. ~~Feature 12: runtime state machine and stall supervision~~ — **COMPLETE**
2. ~~Feature 13: coding operator dashboard and session control~~ — **COMPLETE**
3. ~~Feature 14: multi-feature autonomy hardening~~ — **COMPLETE**
4. ~~Feature 15: context injection and handover quality~~ — **COMPLETE**
5. Feature 16: runtime adapter formalization and early headless transport abstraction
6. Feature 17: rich headless runtime sessions and structured observability
7. Feature 18: learning-loop signal enrichment and governance feedback hardening
8. Feature 19: coding substrate generalization and Agent OS lift-in

---

### Feature 14 completed: Multi-Feature Autonomy Hardening And Chain Recovery

Merged: PRs on `feature/multi-feature-autonomy-hardening` branch, 2026-04-02

Material changes:
- canonical multi-feature chain execution contract defining 8 chain states, deterministic advancement rules, and 6 explicit stop conditions
- chain state projection layer: single queryable surface for chain progression truth, active/next feature, advancement blockers, and carry-forward summary
- recovery decision tree: 3 failure classes (transient/fixable/non-recoverable), max 2 retries per class and 3 total, with deterministic requeue vs escalate
- resume-safe vs resume-unsafe chain state classification
- branch baseline guard: merge-base + is-ancestor check prevents stale worktree drift
- carry-forward ledger: cumulative findings, open items, deferred items, and residual risks across feature boundaries with provenance preservation
- deferred item validation: blocker deferral rejected at code level (contract O-3 enforcement)
- chain residual governance model for final certification requirements
- 130 chain tests passing across 4 test files

Artifacts:
- `docs/MULTI_FEATURE_CHAIN_CONTRACT.md` — chain execution contract
- `docs/CHAIN_RESIDUAL_GOVERNANCE.md` — residual governance model
- `scripts/lib/chain_state_projection.py` — chain state projection and advancement truth
- `scripts/lib/chain_recovery.py` — recovery, requeue, branch guard, carry-forward snapshot
- `tests/test_chain_state_projection.py` — 36 projection tests
- `tests/test_chain_recovery.py` — 39 recovery tests
- `tests/test_chain_carry_forward_certification.py` — 22 carry-forward certification tests
- `tests/test_chain_certification.py` — 22 final certification tests (including 5-feature lifecycle with recovery)

Bug found and fixed during certification:
- `_accumulate_open_items` was overwriting `origin_feature` provenance when updating existing items across feature boundaries — fixed by preserving existing provenance during merge

### Recommended next execution order

1. ~~Feature 12: runtime state machine and stall supervision~~ — **COMPLETE**
2. ~~Feature 13: coding operator dashboard and session control~~ — **COMPLETE**
3. ~~Feature 14: multi-feature autonomy hardening~~ — **COMPLETE**
4. Feature 15: intelligence, context injection, and handover quality
5. Feature 16: runtime adapter formalization and early headless transport abstraction
6. Feature 17: rich headless runtime sessions and structured observability
7. Feature 18: learning-loop signal enrichment and governance feedback hardening
8. Feature 19: coding substrate generalization and Agent OS lift-in

---

### Feature 12 completed: Autonomous Runtime State Machine And Stall Supervision

Merged: PRs #64–#67, 2026-04-01 19:29–19:55 UTC

Material changes:
- canonical worker state machine with 9 explicit states and deterministic transition matrix
- heartbeat/output independence: liveness and progress tracked as separate signals
- stall detection, dead-worker escalation, and anomaly-to-open-item auto-creation
- zombie lease and ghost dispatch tie-break detection
- runtime reliability certification with 129 tests passing

Artifacts:
- `docs/core/130_RUNTIME_STATE_MACHINE_CONTRACT.md` — state machine contract
- `docs/core/131_RUNTIME_RELIABILITY_CERTIFICATION.md` — certification report
- `scripts/lib/worker_state_manager.py` — WorkerStateManager (schema v9)
- `scripts/lib/runtime_supervisor.py` — RuntimeSupervisor with 9 anomaly types
- `schemas/runtime_coordination_v9.sql` — worker_states table

### Feature 13 completed: Coding Operator Dashboard And Session Control

Merged: PRs #68–#72, 2026-04-01 20:05–20:44 UTC

Material changes:
- dashboard read-model contract with 5 operator surfaces and forbidden-path enforcement
- FreshnessEnvelope on every view response with staleness tracking
- 6 safe operator actions (start/stop/attach/refresh/reconcile/inspect) with structured ActionOutcome
- Next.js operator section with terminal status, project cards, aggregate open items
- cross-project open-item visibility with attention model
- degraded-state banner that is unmissable (never silent)

Artifacts:
- `docs/core/140_DASHBOARD_READ_MODEL_CONTRACT.md` — read-model contract
- `docs/core/141_DASHBOARD_CERTIFICATION.md` — certification report
- `scripts/lib/dashboard_read_model.py` — 5 read-model views
- `scripts/lib/dashboard_actions.py` — 6 safe actions
- `dashboard/serve_dashboard.py` — 12 operator API routes
- `dashboard/token-dashboard/app/operator/` — operator UI pages
- `dashboard/token-dashboard/components/operator/` — operator UI components

### Headless review gate correction

During Feature 12/13 execution, headless review gates were not executed:
- PR #64: gates requested but execution failed (gate_runner.py absent at branch-cut time; became available after origin/main merge but T0 did not retry)
- PRs #65–#72: gates were never requested (T0 orchestration flow failure)

Root cause: dual — infrastructure gap at branch creation + T0 flow allowed request-without-execute

Correction applied:
- Codex default changed from disabled to enabled (`VNX_CODEX_HEADLESS_ENABLED` default "1")
- `request-and-execute` atomic CLI command added to review_gate_manager.py
- `t0_gate_enforcement.sh` wrapper created for T0 use
- stall thresholds increased (Gemini 180s, Codex 300s) for agentic CLI behavior
- retroactive Gemini gate completed for PR #64 (contract_hash: e22f5af309c9b65e)
- retroactive Codex execution in progress

### Carry-forward open items from Feature 12/13

All warn severity (no blockers):
- OI-314: `worker_state_manager.py:transition` — 3 lines over threshold
- OI-370: `runtime_supervisor.py:_check_terminal` — 3 lines over threshold
- OI-371: `dashboard_read_model.py:get_aggregate` — 14 lines over threshold
- OI-372: `dashboard_read_model.py:get_items` — 1 line over threshold
- OI-373: `dashboard_actions.py:start_session` — 36 lines over threshold (highest priority refactor)
- OI-374: `serve_dashboard.py` — 415 lines over file threshold (pre-existing, needs route decomposition)

### Recommended next execution order

1. Feature 14: multi-feature autonomy hardening
2. Feature 15: intelligence, context injection, and handover quality
3. Feature 16: runtime adapter formalization / early headless transport abstraction

Prerequisite for Feature 14: live smoke test proving both Gemini and Codex gates execute end-to-end.

### Planning stack formalized

- internal Agent OS document stack established:
  - strategy
  - guardrails
  - autonomous coding roadmap
- strategy docs were moved into internal, gitignored locations under `docs/internal/`
- roadmap sequencing was tightened around:
  - coding-first wedge
  - early runtime abstraction discipline
  - explicit current-state vs target-state framing

### Feature plans added

- Feature 12 plan added:
  - [FEATURE_PLAN_AUTONOMOUS_RUNTIME_STATE_MACHINE_AND_STALL_SUPERVISION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_AUTONOMOUS_RUNTIME_STATE_MACHINE_AND_STALL_SUPERVISION.md)
- Feature 13 plan added:
  - [FEATURE_PLAN_CODING_OPERATOR_DASHBOARD_AND_SESSION_CONTROL.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CODING_OPERATOR_DASHBOARD_AND_SESSION_CONTROL.md)
- Feature 14 plan added:
  - [FEATURE_PLAN_MULTI_FEATURE_AUTONOMY_HARDENING_AND_CHAIN_RECOVERY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_MULTI_FEATURE_AUTONOMY_HARDENING_AND_CHAIN_RECOVERY.md)
- Feature 15 plan added:
  - [FEATURE_PLAN_CONTEXT_INJECTION_AND_HANDOVER_QUALITY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CONTEXT_INJECTION_AND_HANDOVER_QUALITY.md)
- Feature 16 plan added:
  - [FEATURE_PLAN_RUNTIME_ADAPTER_FORMALIZATION_AND_HEADLESS_TRANSPORT_ABSTRACTION.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_RUNTIME_ADAPTER_FORMALIZATION_AND_HEADLESS_TRANSPORT_ABSTRACTION.md)
- Feature 17 plan added:
  - [FEATURE_PLAN_RICH_HEADLESS_RUNTIME_SESSIONS_AND_STRUCTURED_OBSERVABILITY.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_RICH_HEADLESS_RUNTIME_SESSIONS_AND_STRUCTURED_OBSERVABILITY.md)
- Feature 18 plan added:
  - [FEATURE_PLAN_LEARNING_LOOP_SIGNAL_ENRICHMENT_AND_GOVERNANCE_FEEDBACK_HARDENING.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_LEARNING_LOOP_SIGNAL_ENRICHMENT_AND_GOVERNANCE_FEEDBACK_HARDENING.md)
- Feature 19 plan added:
  - [FEATURE_PLAN_CODING_SUBSTRATE_GENERALIZATION_AND_AGENT_OS_LIFT_IN.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/FEATURE_PLAN_CODING_SUBSTRATE_GENERALIZATION_AND_AGENT_OS_LIFT_IN.md)

### Program-management rule introduced

- this internal changelog and the internal project status doc must be updated at the end of every future feature
- the update must happen inside the feature’s certification/final PR, not as an afterthought
- future feature plans must include this requirement explicitly in:
  - scope
  - deliverables
  - quality gates

### Recommended next execution order

1. Feature 12: runtime state machine and stall supervision
2. Feature 13: coding operator dashboard and session control
3. Feature 14: multi-feature autonomy hardening
4. Feature 15: context injection and handover quality
5. Feature 16: runtime adapter formalization / early headless transport abstraction
6. Feature 17: rich headless runtime sessions and structured observability
7. Feature 18: learning-loop signal enrichment and governance feedback hardening
8. Feature 19: coding substrate generalization and Agent OS lift-in

---

## 2026-04-01

### Baseline planning direction sharpened

- Agent OS strategy reframed around:
  - stateful orchestration
  - continuity
  - managers plus bounded workers
  - governance profiles
- coding remained the first practical wedge
- dashboard and runtime work were explicitly placed after runtime-truth hardening, not before it

### Internal status discipline clarified

- internal planning docs were treated as the overkoepelende internal source for:
  - what comes next
  - why it comes next
  - which feature-plan families follow from the roadmap

---

## Maintenance Rule

At the end of every completed future feature, the responsible certification terminal must update:

- [CHANGELOG.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/CHANGELOG.md)
- [PROJECT_STATUS.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/internal/plans/PROJECT_STATUS.md)

Minimum required update content:

- what the feature materially changed
- what new capability is now proven
- what open items or residual risks remain
- what the next recommended feature order is after completion
