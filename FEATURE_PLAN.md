# Feature: Autonomous Runtime State Machine And Stall Supervision

**Feature-ID**: Feature 12

**Status**: Planned
**Priority**: P1
**Branch**: `feature/autonomous-runtime-state-machine-and-stall-supervision`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Eliminate silent worker bad states and establish one explicit runtime truth model that T0, later dashboard work, and future runtime adapters can trust.

Execution context:
- first post-bridge product-level feature after B1-B3 and Feature 10-11
- maps directly to Roadmap M1: Autonomous Runtime Reliability
- should land before the first coding operator dashboard feature so the dashboard does not build on ambiguous runtime state

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because runtime truth is merge-critical
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

The current system still allows bad runtime ambiguity:
- workers can end in idle-like or unknown states after bad exits
- no-output or stale sessions can persist too long before becoming explicit governance objects
- T0 and operators can still infer runtime truth indirectly instead of reading a canonical state machine
- future dashboard and runtime adapter work would deepen tmux-era ambiguity if this layer remains implicit

## Design Goal

Create a deterministic worker/session state model with heartbeat, stall classification, and open-item escalation so silent runtime failure becomes structurally impossible.

## Non-Goals

- no dashboard UI in this feature
- no broad transport rewrite
- no business-domain manager/worker generalization yet
- no attempt to solve all context-injection or learning-loop issues here

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- local receipts alone are not sufficient for progression if GitHub CI is red or missing

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
```

## PR-0: Runtime State Machine Contract And Operator Truth Rules
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical worker/session lifecycle, heartbeat semantics, tie-break rules across queue/runtime/terminal surfaces, and the escalation policy for anomalous runtime states.

### Scope
- define canonical runtime states such as `working`, `idle`, `stalled`, `exited`, `blocked`, `awaiting_input`, `resume_unsafe`
- define heartbeat freshness vs stale thresholds
- define no-output detection semantics for headless and interactive workers
- define which surface wins under mismatch between runtime, queue, and terminal activity
- define when anomalous runtime situations create open items automatically

### Deliverables
- runtime state machine contract document
- operator truth and tie-break policy
- anomaly classification matrix
- GitHub PR with linked acceptance summary

### Success Criteria
- runtime states are explicit and finite
- stale vs active vs broken states are no longer implicit heuristics
- T0 has deterministic guidance for which state surface to trust
- anomaly escalation is defined before implementation starts

### Quality Gate
`gate_pr0_runtime_state_machine_contract`:
- [ ] Contract defines canonical worker and session states with allowed transitions
- [ ] Contract defines heartbeat freshness, stale thresholds, and no-output semantics
- [ ] Contract defines deterministic tie-break rules across queue, runtime, and terminal surfaces
- [ ] Contract defines automatic open-item creation for anomalous runtime states
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Session State Machine And Heartbeat Persistence
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-0]

### Description
Implement the canonical state machine and heartbeat persistence so workers cannot silently slip from active execution into ambiguous idle or unknown states.

### Scope
- add canonical runtime state representation in the execution layer
- persist heartbeat timestamps and last-output timestamps
- implement deterministic state transitions on launch, active output, clean exit, bad exit, and manual interruption
- expose canonical runtime state for T0 and downstream read-model consumers
- add tests for state transitions and stale classification

### Deliverables
- runtime state model implementation
- heartbeat persistence and last-output tracking
- state transition tests
- GitHub PR with evidence summary

### Success Criteria
- runtime state no longer depends on ad hoc terminal inference alone
- last heartbeat and last output are durable and queryable
- clean vs bad exits resolve to explicit states
- tests cover the main transition paths

### Quality Gate
`gate_pr1_state_machine_and_heartbeat`:
- [ ] All runtime state machine and heartbeat tests pass
- [ ] Launch, output, clean exit, bad exit, and interruption transitions are explicit under test
- [ ] Heartbeat and last-output timestamps persist in canonical runtime state
- [ ] T0-readable state surface exists without scraping terminal behavior ad hoc
- [ ] GitHub PR exists with implementation and evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Stall Detection, Exit Classification, And Open-Item Escalation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @monitoring-specialist
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-1]

### Description
Add runtime supervision that detects no-output hangs, stale sessions, and bad exits, then emits durable evidence and governance objects instead of leaving T0 to discover failures manually.

### Scope
- implement no-output stall detection
- classify exit paths into operator-meaningful runtime outcomes
- create durable audit records for stall, stale, and bad-exit scenarios
- auto-create open items for unresolved runtime anomalies
- add tests for stall detection, bad exits, and escalation paths

### Deliverables
- stall and stale supervision logic
- exit classification mapping
- runtime anomaly audit records
- open-item escalation path for unresolved runtime failures
- GitHub PR with failure-path evidence summary

### Success Criteria
- silent stalls become explicit runtime events
- bad exits no longer collapse into ambiguous idle states
- unresolved runtime anomalies create open items automatically
- runtime failures become visible inputs for later dashboard work

### Quality Gate
`gate_pr2_runtime_supervision_and_escalation`:
- [ ] All runtime supervision tests pass
- [ ] No-output stall detection produces structured runtime outcomes under test
- [ ] Bad exits classify into explicit operator-meaningful states under test
- [ ] Unresolved runtime anomalies create durable open items automatically
- [ ] Audit records exist for stall, stale, and bad-exit paths
- [ ] GitHub PR exists with failure-path evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Unattended Runtime Reliability Certification
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-2]

### Description
Certify that the runtime layer now surfaces active, stale, stalled, blocked, and exited states deterministically enough for unattended coding runs and future dashboard work.

### Scope
- run unattended scenarios that previously produced mystery-idle or silently stale behavior
- verify runtime truth remains reconstructable from evidence
- verify anomalous runtime situations produce open items and operator-readable diagnostics
- verify each prior PR was merged from green GitHub CI before the next PR began
- require Gemini review and Codex final gate on the certification PR

### Deliverables
- unattended runtime certification report
- scenario evidence for active, stalled, stale, blocked, and exited outcomes
- sequencing audit for GitHub PR progression and green-CI compliance
- residual risk summary for follow-on Feature 13

### Success Criteria
- the operator can tell when a worker is truly working vs stale vs stalled vs broken
- prior silent-failure patterns are reproducible and closed under test
- this feature closes with zero unresolved chain-created open items
- downstream dashboard work can trust runtime state as a real substrate, not a cosmetic proxy

### Quality Gate
`gate_pr3_runtime_reliability_certification`:
- [ ] All unattended runtime certification tests pass
- [ ] Active, stalled, stale, blocked, and exited outcomes are distinguishable under test and in evidence
- [ ] Previously observed silent-runtime failure patterns are closed under test
- [ ] Each PR in this feature was merged only after green GitHub CI on the corresponding GitHub PR
- [ ] Certification report records residual risks for follow-on dashboard work
- [ ] Feature closes with zero unresolved chain-created open items
- [ ] GitHub PR exists with certification evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
