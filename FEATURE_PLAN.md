# Feature: Rich Headless Runtime Sessions And Structured Observability

**Feature-ID**: Feature 17
**Status**: Planned
**Priority**: P1
**Branch**: `feature/rich-headless-runtime-sessions-and-structured-observability`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Turn the early headless abstraction into a governed runtime path with structured session lifecycle, explicit attempt/session observability, and auditable execution evidence that is richer than plain stdout/stderr capture.

Execution context:
- intended follow-on after Feature 16 runtime adapter formalization
- maps primarily to Roadmap M5b: Richer Headless Runtime Implementations
- assumes `RuntimeAdapter`, `TmuxAdapter`, and the early headless/local-session boundary already exist and are stable enough to extend
- assumes Feature 14 chain recovery and Feature 15 context/handover quality have already reduced chain noise enough that richer headless runtime evidence will be actionable

Execution preconditions:
- Feature 16 runtime adapter formalization baseline must be merged first
- both Gemini and Codex headless gates must remain operational on current `main` throughout this feature
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- Feature 15 bounded context and handover payloads must already exist so headless sessions can consume structured context rather than raw narrative spill

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because runtime/session observability defects can silently invalidate future autonomous evidence
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

The system now has a runtime adapter boundary, but the headless path is still too thin:
- headless execution evidence is still dominated by stdout/stderr and terminal end states
- session and attempt lifecycle is not yet rich enough to support serious unattended operator trust
- structured event streams for headless workers are not strong enough to support future retrospective learning and policy hardening
- richer headless execution risks becoming opaque if observability is not designed into the runtime path itself

## Design Goal

Create a production-grade headless runtime path with explicit session/attempt lifecycle, structured observability, and auditable execution artifacts that can support future chain autonomy, learning loops, and operator trust without pretending every CLI exposes identical internal detail.

## Non-Goals

- no full tmux removal
- no remote-control channel architecture
- no broad Business OS rollout
- no attempt to guarantee universal tool-call capture for providers that do not expose it
- no automatic policy mutation from learning output in this feature

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- headless observability claims must be backed by retained structured artifacts and certification evidence
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Headless Runtime Session Contract And Observability Schema
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical session/attempt lifecycle, structured event schema, and evidence expectations for richer headless runtime execution.

### Scope
- define headless session, attempt, and run identity model
- define canonical state machine for headless session lifecycle
- define structured event schema for start, progress, completion, timeout, failure, and attachability signals
- define evidence classes: raw output, structured event stream, report artifact, and runtime correlation metadata
- define explicit requirements and limits for tool-call observability based on provider capability

### Deliverables
- headless runtime session contract
- structured observability schema
- provider-capability matrix for tool-call visibility vs output-only visibility
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- headless runtime lifecycle is explicit before implementation starts
- structured observability requirements are locked before runtime changes land
- provider limitations around tool-call visibility are made explicit instead of hand-waved
- future learning-loop work has a stable session/attempt evidence model to consume

### Quality Gate
`gate_pr0_headless_runtime_session_contract`:
- [ ] Contract defines headless session, attempt, and run identity model
- [ ] Contract defines explicit headless session lifecycle states and transitions
- [ ] Contract defines structured event schema for start, progress, timeout, completion, and failure
- [ ] Contract defines evidence classes and provider capability limits for tool-call visibility
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: LocalSessionAdapter Lifecycle And Attempt Tracking
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Implement richer headless session lifecycle and attempt tracking behind the adapter boundary so headless runs are represented as real runtime sessions rather than thin subprocess side-effects.

### Scope
- implement explicit LocalSessionAdapter session lifecycle against the new contract
- add attempt tracking and session/run correlation metadata
- persist lifecycle transitions into canonical runtime state
- add tests for session creation, attempt rollover, timeout, completion, and abnormal exit handling

### Deliverables
- LocalSessionAdapter lifecycle implementation
- attempt/session tracking
- runtime persistence integration
- GitHub PR with lifecycle evidence summary

### Success Criteria
- headless runs are represented as explicit sessions and attempts under test
- runtime truth can distinguish session lifecycle states instead of inferring from final output
- abnormal exits and retries are correlated to the correct session/attempt identity
- richer headless runtime no longer depends on ad hoc subprocess interpretation alone

### Quality Gate
`gate_pr1_local_session_adapter_lifecycle`:
- [ ] All LocalSessionAdapter lifecycle tests pass
- [ ] Headless sessions and attempts are persisted and queryable under test
- [ ] Timeout, retry, abnormal exit, and completion map to explicit lifecycle states under test
- [ ] Runtime truth preserves correct session/attempt correlation under test
- [ ] GitHub PR exists with lifecycle evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Structured Headless Event Stream And Artifact Correlation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Add a structured headless runtime event stream and correlate it with session/attempt artifacts so operators and downstream systems can inspect one coherent execution timeline.

### Scope
- emit structured events for session start, subprocess launch, progress heartbeat, completion, timeout, failure, and artifact materialization
- persist correlation keys linking event stream, runtime state, and artifacts
- expose event timeline in a stable machine-readable form
- add tests for correlation integrity and event-stream completeness

### Deliverables
- structured headless event stream
- artifact/session correlation model
- event-stream validation tests
- GitHub PR with observability evidence summary

### Success Criteria
- a headless run can be reconstructed from one coherent event stream under test
- artifacts and runtime state point to the same session/attempt identity
- structured observability becomes stronger than raw stdout/stderr capture alone
- operator and downstream analysis tools gain one stable timeline surface

### Quality Gate
`gate_pr2_structured_headless_event_stream`:
- [ ] All structured event stream and correlation tests pass
- [ ] Session lifecycle events are emitted in canonical order under test
- [ ] Artifact and runtime-state correlation keys remain consistent under test
- [ ] Machine-readable headless timeline can be reconstructed under test
- [ ] GitHub PR exists with observability evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Provider-Aware Tool Visibility And Progress Projections
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Add provider-aware visibility rules so VNX records structured tool-call or progress detail when a provider exposes it, while remaining explicit and honest when only coarse output-level observability is available.

### Scope
- implement provider capability flags for tool-call visibility, structured progress events, and output-only fallback
- expose these capabilities to runtime/read-model consumers
- add projections for attachability, progress confidence, and observability quality
- add tests covering Gemini, Codex, and output-only fallback semantics

### Deliverables
- provider-aware observability capability layer
- progress and observability-quality projections
- provider capability tests
- GitHub PR with provider-visibility evidence summary

### Success Criteria
- VNX stops pretending all providers expose the same internal detail
- tool visibility and output-only fallback are both explicit and queryable under test
- operators can tell whether they are seeing rich progress or coarse output-only evidence
- future learning loops gain cleaner distinctions between runtime weakness and provider limitation

### Quality Gate
`gate_pr3_provider_aware_tool_visibility`:
- [ ] All provider visibility and fallback tests pass
- [ ] Structured tool/progress visibility is exposed when supported under test
- [ ] Output-only fallback remains explicit and honest under test
- [ ] Observability-quality projection distinguishes provider capability levels under test
- [ ] GitHub PR exists with provider-visibility evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Rich Headless Runtime Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify that the richer headless runtime path is auditable, provider-aware, and strong enough to support future unattended autonomy and learning-loop work.

### Scope
- certify session/attempt lifecycle correctness under success, timeout, and failure scenarios
- certify event-stream and artifact correlation integrity
- certify provider-aware observability claims and fallback honesty
- certify that planning/status docs are updated for the new baseline

### Deliverables
- headless runtime certification report
- retained evidence for session lifecycle and event-stream integrity
- updated internal planning docs (`CHANGELOG.md`, `PROJECT_STATUS.md`)
- GitHub PR with certification verdict

### Success Criteria
- richer headless runtime behavior is proven with retained evidence
- provider capability limits are explicit rather than hidden
- structured observability is good enough to support later learning-loop hardening
- planning docs reflect the new post-Feature-17 baseline

### Quality Gate
`gate_pr4_rich_headless_runtime_certification`:
- [ ] Certification covers success, timeout, retry, and failure lifecycle scenarios
- [ ] Certification proves structured event stream and artifact correlation integrity
- [ ] Certification proves provider-aware observability and explicit fallback semantics
- [ ] `docs/internal/plans/CHANGELOG.md` updated with Feature 17 closeout
- [ ] `docs/internal/plans/PROJECT_STATUS.md` updated with Feature 17 status and next-order recommendation
- [ ] GitHub PR exists with certification verdict
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
