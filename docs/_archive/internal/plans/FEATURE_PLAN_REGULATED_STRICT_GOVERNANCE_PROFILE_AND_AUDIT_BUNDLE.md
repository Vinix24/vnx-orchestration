# Feature: Regulated-Strict Governance Profile And Audit Bundle

**Feature-ID**: Feature 21
**Status**: Planned
**Priority**: P1
**Branch**: `feature/regulated-strict-governance-profile-and-audit-bundle`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Define and pilot the `regulated_strict` governance profile with explicit approvals, auditable evidence bundles, and strict closure semantics.

Execution context:
- intended follow-on after Feature 20 business_light pilot
- maps to the post-19 pilot families in Agent OS Strategy (regulated workflow profile plans)
- assumes coding_strict remains authoritative and business_light pilot is bounded
- focuses on audit-bundle readiness, not a full regulated-domain rollout

Execution preconditions:
- Features 18 through 20 must already be merged first
- both Gemini and Codex headless gates must remain operational on current `main` throughout this feature
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- regulated_strict must not weaken coding_strict or business_light behavior

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because audit-bundle defects can silently invalidate regulated evidence
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

Pilot override (Features 18–22 chain only):
- Gemini headless review is disabled for this pilot due to rate limits.
- Codex headless gate remains required on every PR.
- This exception must be recorded in `CHAIN_PILOT_18_22_REPORT.md`.

## Problem Statement

Regulated workflows require explicit approvals and durable evidence bundles:
- current evidence artifacts are rich but not packaged into audit-ready bundles
- approvals are implicit rather than explicit steps
- regulated_strict governance rules are not yet codified or tested
- without a pilot, regulated-domain planning remains speculative

## Design Goal

Deliver a regulated_strict governance profile with explicit approvals, audit-bundle packaging, and strict closure semantics while preserving the coding-first core and business_light pilot boundaries.

## Non-Goals

- no real regulated-domain rollout
- no external compliance integrations
- no replacing coding_strict or business_light workflows
- no autonomous approval without operator confirmation

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- audit bundles must be reproducible from retained evidence
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Regulated-Strict Governance Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define regulated_strict governance rules, explicit approval semantics, and audit-bundle requirements.

### Scope
- define regulated_strict governance rules and approval steps
- define audit-bundle composition and evidence requirements
- define closure semantics (no implicit closeouts)
- define non-goals and rollback criteria

### Deliverables
- regulated_strict governance contract
- audit-bundle requirements
- approval semantics
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- regulated_strict rules are explicit before implementation
- approval steps are unambiguous and auditable
- audit-bundle scope is locked
- rollback criteria are defined

### Quality Gate
`gate_pr0_regulated_strict_contract`:
- [ ] Contract defines regulated_strict governance and approval semantics
- [ ] Audit-bundle composition is explicit
- [ ] Closure semantics prohibit implicit closeouts
- [ ] Rollback criteria are defined
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Approval Workflow And Evidence Capture
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Implement explicit approval steps and evidence capture required by regulated_strict workflows.

### Scope
- implement explicit approval checkpoints for regulated_strict dispatches
- capture approval identity, timestamp, and decision metadata
- ensure approvals are required before closeout
- add tests for approval enforcement

### Deliverables
- approval workflow implementation
- approval metadata capture
- approval enforcement tests
- GitHub PR with approval evidence summary

### Success Criteria
- regulated_strict dispatches cannot close without approval under test
- approval metadata is retained and queryable
- approvals are auditable for future bundles
- coding_strict and business_light behavior remain unchanged

### Quality Gate
`gate_pr1_regulated_approval_workflow`:
- [ ] Approval enforcement tests pass
- [ ] Approval metadata is retained under test
- [ ] Regulated dispatches cannot close without approval under test
- [ ] GitHub PR exists with approval evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Audit Bundle Builder And Evidence Index
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Package evidence into audit bundles with an explicit index so regulated_strict runs can be audited without manual reconstruction.

### Scope
- implement audit bundle builder (receipts, gates, runtime events, approvals)
- generate immutable evidence index for each regulated_strict run
- add tests for bundle integrity and index completeness
- ensure bundle creation is non-destructive to existing evidence

### Deliverables
- audit bundle builder
- evidence index format
- bundle integrity tests
- GitHub PR with audit-bundle evidence summary

### Success Criteria
- audit bundles are reproducible and complete under test
- evidence index makes audit scope explicit
- bundles preserve existing evidence without mutation
- regulated_strict readiness becomes concrete

### Quality Gate
`gate_pr2_audit_bundle_builder`:
- [ ] Bundle integrity tests pass
- [ ] Evidence index is complete under test
- [ ] Bundles preserve evidence without mutation
- [ ] GitHub PR exists with audit-bundle evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Regulated-Strict Dashboard Surface
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @frontend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Expose regulated_strict status and audit bundle visibility in the operator dashboard without mixing profiles.

### Scope
- add regulated_strict status indicators
- surface approval state and bundle readiness
- prevent profile mixing or implicit downgrade
- add tests for regulated_strict visibility and isolation

### Deliverables
- regulated_strict dashboard surface
- approval/bundle visibility
- isolation tests
- GitHub PR with dashboard evidence summary

### Success Criteria
- operators can see regulated_strict status and bundle readiness
- profile boundaries remain explicit under test
- regulated_strict never silently downgrades to lighter governance
- dashboard surfaces remain read-model compliant

### Quality Gate
`gate_pr3_regulated_dashboard_surface`:
- [ ] Regulated_strict dashboard visibility tests pass
- [ ] Approval and bundle readiness are visible under test
- [ ] Profile boundaries remain explicit under test
- [ ] GitHub PR exists with dashboard evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Regulated-Strict Pilot Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify the regulated_strict profile and audit bundle readiness with retained evidence.

### Scope
- certify approval workflow and bundle integrity
- certify dashboard visibility and profile isolation
- update planning docs (`CHANGELOG.md`, `PROJECT_STATUS.md`)

### Deliverables
- regulated_strict certification report
- retained audit-bundle evidence
- updated planning docs
- GitHub PR with certification verdict

### Success Criteria
- regulated_strict pilot is validated with retained evidence
- audit bundle readiness is proven under test
- profile isolation is preserved
- planning docs reflect the new pilot baseline

### Quality Gate
`gate_pr4_regulated_strict_certification`:
- [ ] Certification covers approval workflow and bundle integrity
- [ ] Regulated_strict dashboard visibility verified
- [ ] `docs/internal/plans/CHANGELOG.md` updated with Feature 21 closeout
- [ ] `docs/internal/plans/PROJECT_STATUS.md` updated with Feature 21 status and next-order recommendation
- [ ] GitHub PR exists with certification verdict
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
