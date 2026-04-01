# Feature: Coding Operator Dashboard And Session Control

**Feature-ID**: Feature 13

**Status**: Planned
**Priority**: P1
**Branch**: `feature/coding-operator-dashboard-and-session-control`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Deliver the first coding operator dashboard that can start sessions per project, show which terminals are actually working, and expose open items per project and across projects from a read-model-backed control surface.

Execution context:
- immediate follow-on feature after Feature 12 runtime reliability work
- maps primarily to Roadmap M4: Coding Operator Dashboard
- incorporates early M5a discipline by avoiding new direct tmux coupling and routing all operator visibility through stable read-model surfaces

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because operator control surfaces must not regress governance truth
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

The current operator experience is still fragmented:
- session truth is spread across tmux, receipts, runtime artifacts, and ad hoc scripts
- open items are not yet easy to inspect per project and in aggregate from one practical surface
- starting or attaching to sessions still depends too much on manual terminal choreography
- a dashboard built directly on scripts or raw files would become a cosmetic surface instead of an operator-grade control plane

## Design Goal

Create a read-model-backed coding dashboard that makes live session state, project-level open items, aggregate open items, and safe session actions visible in one practical operator surface.

## Non-Goals

- no broad Business OS rollout
- no hosted multi-user control plane
- no replacement of tmux as the active runtime adapter in this feature
- no heavy intelligence or learning-loop overhaul beyond what the dashboard needs to render trustworthy operator state

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- no dashboard PR may merge against ambiguous runtime semantics from Feature 12

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Dashboard Read Model And Operator Surface Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the dashboard data contract, operator questions, safe actions, and read-model boundaries so the UI is forced to sit on canonical projected state rather than direct script calls or raw file parsing.

### Scope
- define the first dashboard surfaces: projects, sessions, terminals, open items, aggregate open items
- define mandatory operator questions each surface must answer
- define safe first actions: session start, attach/open terminal, refresh/reconcile, inspect open items
- define required read-model inputs and forbidden direct data paths
- define empty, stale, and degraded-state handling

### Deliverables
- dashboard read-model and operator-surface contract
- safe-action policy for first release
- degraded-state and empty-state policy
- GitHub PR with contract summary

### Success Criteria
- dashboard scope is explicit and practical
- UI-first drift is blocked before implementation starts
- operator questions are known up front
- safe action boundaries are explicit

### Quality Gate
`gate_pr0_dashboard_contract`:
- [ ] Contract defines first dashboard surfaces and operator questions
- [ ] Contract defines required read-model inputs and forbids raw script-coupled rendering
- [ ] Contract defines safe initial actions and degraded-state handling
- [ ] Contract defines per-project and cross-project open-item visibility requirements
- [ ] GitHub PR exists with contract summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Runtime, Session, And Open-Item Read Model Projections
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-0]

### Description
Implement the read-model projections that unify runtime state, project session truth, and open-item visibility into one backend surface the dashboard can trust.

### Scope
- project-level session projection
- terminal/runtime projection using Feature 12 canonical state
- per-project open-item projection
- aggregate open-item projection across projects
- tests for projection freshness, degraded state, and mismatch diagnostics

### Deliverables
- dashboard read-model projection layer
- projection tests
- operator-readable mismatch diagnostics
- GitHub PR with projection evidence summary

### Success Criteria
- dashboard-facing state can be queried without scraping raw runtime files manually
- project and aggregate open-item views are available from one stable surface
- runtime and session truth remain explainable under degraded conditions
- projection tests cover stale and mismatch scenarios

### Quality Gate
`gate_pr1_dashboard_read_model`:
- [ ] All dashboard read-model projection tests pass
- [ ] Project session, terminal runtime, and open-item projections are queryable from one stable surface
- [ ] Aggregate open-item projection across projects is available under test
- [ ] Degraded or mismatched projection states produce operator-readable diagnostics
- [ ] GitHub PR exists with projection evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Session Start And Safe Operator Control Actions
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-1]

### Description
Implement the first safe operator control actions so sessions can be started per project and the operator can reach the correct terminal/session without unsafe direct orchestration shortcuts.

### Scope
- per-project session start action
- safe attach/open-terminal action for active sessions
- explicit action outcomes and operator-visible error states
- no action may bypass runtime/read-model truth
- tests for session start, attach intent, and degraded-action behavior

### Deliverables
- session start and attach control handlers
- operator-visible action outcome model
- action tests for success and degraded paths
- GitHub PR with control-surface evidence summary

### Success Criteria
- an operator can start a session per project from one clear surface
- terminal access is guided by explicit session truth, not guesswork
- action failures are visible and auditable
- no action path silently bypasses governance or runtime truth

### Quality Gate
`gate_pr2_dashboard_control_actions`:
- [ ] All session-control action tests pass
- [ ] Session start works per project from the dashboard-backed control surface
- [ ] Attach/open-terminal action resolves against canonical session truth under test
- [ ] Failed or degraded actions produce explicit operator-visible outcomes
- [ ] No action path bypasses runtime/read-model truth under test
- [ ] GitHub PR exists with control-surface evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Dashboard UI For Projects, Sessions, Terminals, And Open Items
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-2]

### Description
Build the first operator dashboard UI on top of the read model so the coding operator can see project sessions, terminal state, and open items without dropping into manual tmux and raw file inspection for routine decisions.

### Scope
- projects overview with session start entrypoint
- active sessions and terminal status view
- per-project open-item view
- aggregate open-items view across projects
- degraded-state, empty-state, and stale-state rendering
- UI tests for main operator flows

### Deliverables
- dashboard UI for projects, sessions, terminals, and open items
- operator flow tests
- degraded-state rendering coverage
- GitHub PR with screenshots and flow evidence

### Success Criteria
- the operator can see which terminals are actually working
- per-project and aggregate open items are visible from one dashboard
- starting a project session is practical from the UI
- degraded state is explicit instead of silently misleading

### Quality Gate
`gate_pr3_dashboard_ui`:
- [ ] All dashboard UI and operator-flow tests pass
- [ ] Projects view shows session start entrypoints and active session visibility
- [ ] Terminal state view distinguishes active, stale, blocked, and exited sessions under test
- [ ] Per-project and aggregate open-item views render correctly under test
- [ ] Degraded, stale, and empty states render explicitly and do not masquerade as healthy state
- [ ] GitHub PR exists with screenshots and operator-flow evidence
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Coding Operator Dashboard Certification
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-3]

### Description
Certify that the first coding operator dashboard is trustworthy enough for daily use, that it improves practical control over autonomous coding flows, and that it did not regress governance truth.

### Scope
- verify per-project session start end to end
- verify active terminal visibility against real runtime state
- verify per-project and aggregate open-item visibility
- verify degraded-state handling under stale or mismatched projections
- verify each prior PR was merged from green GitHub CI before the next PR began
- require Gemini review and Codex final gate on the certification PR

### Deliverables
- dashboard certification report
- operator runbook for first release
- evidence for project session start, active terminal visibility, and open-item views
- sequencing audit for GitHub PR progression and green-CI compliance

### Success Criteria
- the dashboard is useful for real daily operation, not just demonstration
- session start and terminal visibility work against trustworthy state
- open-item visibility is materially better than the current fragmented operator experience
- this feature closes with zero unresolved chain-created open items

### Quality Gate
`gate_pr4_dashboard_certification`:
- [ ] All dashboard certification tests pass
- [ ] End-to-end evidence proves per-project session start works from the dashboard
- [ ] Active terminal visibility matches canonical runtime truth under test
- [ ] Per-project and aggregate open-item views are correct under test
- [ ] Degraded-state handling stays explicit under stale or mismatched projection conditions
- [ ] Each PR in this feature was merged only after green GitHub CI on the corresponding GitHub PR
- [ ] Operator runbook exists for first-release daily use
- [ ] Feature closes with zero unresolved chain-created open items
- [ ] GitHub PR exists with certification evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
