# Feature: Business-Light Governance Pilot And Folder-Scoped Orchestration

**Feature-ID**: Feature 20
**Status**: Planned
**Priority**: P1
**Branch**: `feature/business-light-governance-pilot-and-folder-scoped-orchestration`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Pilot the first `business_light` governance profile with folder-scoped manager/worker orchestration, while preserving the coding-first substrate as authoritative and unchanged.

Execution context:
- intended follow-on after Feature 19 Agent OS lift-in boundary is explicit
- maps to the post-19 pilot families in Agent OS Strategy (business manager/worker pilots)
- assumes coding_strict remains the default profile and is not weakened
- focuses on folder-scoped workspaces, not repo worktree execution

Execution preconditions:
- Features 18 and 19 must already be merged first
- both Gemini and Codex headless gates must remain operational on current `main` throughout this feature
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- business_light rules remain non-authoritative over coding_strict outcomes

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because governance-profile mistakes can silently misroute work
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

Pilot override (Features 18–22 chain only):
- Gemini headless review is disabled for this pilot due to rate limits.
- Codex headless gate remains required on every PR.
- This exception must be recorded in `CHAIN_PILOT_18_22_REPORT.md`.

## Problem Statement

VNX has a strong coding_strict substrate but lacks a validated business_light profile:
- folder-scoped orchestration is not yet formalized
- governance assumptions for business workflows are implicit and untested
- manager/worker behavior for non-coding tasks risks drifting into ad hoc routing
- preferences and lessons are not yet tailored to lighter governance without losing auditability

## Design Goal

Define and implement a business_light governance pilot that proves folder-scoped orchestration, lighter review-by-exception, and explicit boundaries against coding_strict without diluting the coding-first core.

## Non-Goals

- no broad Business OS rollout
- no replacement of coding_strict governance
- no Telegram or external control-plane integration
- no automatic policy mutation without explicit operator approval

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- pilot behavior must be explicitly bounded and reversible
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Business-Light Governance Profile Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the business_light governance profile, folder-scope rules, and authority boundaries relative to coding_strict.

### Scope
- define business_light governance rules (review-by-exception, softer close gates)
- define folder-scoped manager/worker boundaries and context sources
- define boundaries that prevent business_light from changing coding_strict decisions
- define explicit pilot limits and rollback criteria

### Deliverables
- business_light governance profile contract
- folder-scoped orchestration boundary rules
- pilot limits and rollback criteria
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- governance profile is explicit before implementation
- folder-scoped work is formally bounded
- coding_strict authority remains protected
- pilot rollback conditions are explicit

### Quality Gate
`gate_pr0_business_light_contract`:
- [ ] Contract defines business_light governance rules and review-by-exception policy
- [ ] Folder-scoped context sources and boundaries are explicit
- [ ] Coding_strict authority boundaries are explicit
- [ ] Pilot rollback criteria are defined
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Folder-Scoped Manager/Worker Orchestration Layer
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Implement the folder-scoped orchestration model and derive dispatch context from folder-local sources.

### Scope
- define folder-scope resolution rules (root + subfolder)
- implement folder-scoped context assembly with bounded inputs
- ensure folder workflows cannot read coding worktrees by default
- add tests for scope resolution and isolation guarantees

### Deliverables
- folder-scope resolution and context assembly
- isolation rules for coding vs business scopes
- folder-scope tests
- GitHub PR with scope evidence summary

### Success Criteria
- folder-scoped dispatches resolve to correct scope under test
- context remains bounded and isolated from coding worktrees
- scope rules are stable for later business pilots
- isolation prevents accidental coding leakage into business tasks

### Quality Gate
`gate_pr1_folder_scope_orchestration`:
- [ ] Folder-scope resolution tests pass
- [ ] Context assembly is bounded and isolated under test
- [ ] Coding worktree isolation is preserved under test
- [ ] GitHub PR exists with scope evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Business-Light Review And Closeout Policy
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Implement the softer review-by-exception policy for business_light while preserving auditability and open-item continuity.

### Scope
- implement review-by-exception gating for business_light tasks
- keep audit artifacts and open-item continuity intact
- ensure no automatic closeout without explicit manager decision
- add tests for review policy and audit retention

### Deliverables
- business_light review policy implementation
- audit retention and open-item continuity
- review policy tests
- GitHub PR with policy evidence summary

### Success Criteria
- business_light tasks can progress with lighter reviews without losing evidence
- audit artifacts remain available under test
- open items remain continuity-safe under test
- no silent auto-closeouts

### Quality Gate
`gate_pr2_business_light_review_policy`:
- [ ] Review policy tests pass
- [ ] Audit artifacts retained under test
- [ ] Open-item continuity preserved under test
- [ ] No auto-closeout without manager decision under test
- [ ] GitHub PR exists with policy evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Pilot Manager Surface And Governance Profile Selector
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Expose business_light profile selection and status visibility without diluting coding_strict defaults.

### Scope
- add governance profile selector for non-coding scopes
- expose business_light status and open items in operator surface
- prevent business_light from overriding coding_strict decision surfaces
- add tests for profile visibility and isolation

### Deliverables
- governance profile selector (business_light only)
- profile visibility surface
- isolation tests
- GitHub PR with UI evidence summary

### Success Criteria
- operator can see business_light profile without losing coding visibility
- profile boundaries are explicit under test
- coding_strict remains default and authoritative
- governance profile selection is auditable

### Quality Gate
`gate_pr3_business_light_profile_surface`:
- [ ] Profile selector and isolation tests pass
- [ ] Business_light visibility is explicit under test
- [ ] Coding_strict remains default and authoritative under test
- [ ] GitHub PR exists with UI evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Business-Light Pilot Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify the business_light pilot with retained evidence, isolation checks, and rollback readiness.

### Scope
- certify folder-scoped orchestration and isolation
- certify review-by-exception policy and audit retention
- certify governance profile selector behavior
- update planning docs (`CHANGELOG.md`, `PROJECT_STATUS.md`)

### Deliverables
- business_light pilot certification report
- retained evidence for scope isolation and audit retention
- updated planning docs
- GitHub PR with certification verdict

### Success Criteria
- business_light pilot is validated with retained evidence
- coding_strict authority remains intact under test
- rollback criteria are verified and documented
- planning docs reflect new pilot baseline

### Quality Gate
`gate_pr4_business_light_pilot_certification`:
- [ ] Certification covers scope isolation and review policy behavior
- [ ] Audit retention evidence retained under test
- [ ] Governance profile selector behavior verified
- [ ] `docs/internal/plans/CHANGELOG.md` updated with Feature 20 closeout
- [ ] `docs/internal/plans/PROJECT_STATUS.md` updated with Feature 20 status and next-order recommendation
- [ ] GitHub PR exists with certification verdict
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
