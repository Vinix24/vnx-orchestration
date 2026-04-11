# Feature: Coding Substrate Generalization And Agent OS Lift-In

**Feature-ID**: Feature 19
**Status**: Planned
**Priority**: P1
**Branch**: `feature/coding-substrate-generalization-and-agent-os-lift-in`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Generalize the now-stable coding-first manager/worker substrate into an explicit lift-in layer for broader Agent OS use, without prematurely executing a full business-domain rollout.

Execution context:
- intended follow-on after Features 17 and 18
- maps primarily to Roadmap M6: Toward Agent OS Lift-In
- assumes coding runtime, dashboard, chain recovery, context quality, runtime adapters, richer headless runtime, and stronger governance feedback all already exist as stable coding-first foundations
- aims to prepare the broader manager/worker substrate, not yet launch the full Business OS implementation

Execution preconditions:
- Features 14 through 18 must already be merged first
- both Gemini and Codex headless gates must remain operational on current `main` throughout this feature
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- broader Agent OS work must continue to preserve the coding-first wedge and may not destabilize the coding substrate

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because substrate-generalization mistakes can reopen core architecture choices across the program
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

Pilot override (Features 18–22 chain only):
- Gemini headless review is disabled for this pilot due to rate limits.
- Codex headless gate remains required on every PR.
- This exception must be recorded in `CHAIN_PILOT_18_22_REPORT.md`.

## Problem Statement

The coding-first VNX substrate is becoming strong enough to generalize, but the bridge to broader Agent OS use is still implicit:
- reusable manager/worker patterns are spread across coding-specific assumptions
- future business or regulated domains would still need to rediscover core substrate boundaries
- coding abstractions are not yet packaged as a stable lift-in layer
- broader Agent OS expansion remains risky until the reusable substrate is made explicit without diluting the coding core

## Design Goal

Define and implement the first reusable lift-in layer that captures stable coding-first manager/worker abstractions, capability profiles, and governance boundaries so future non-coding plan families can build on them without reopening core architecture.

## Non-Goals

- no full Business OS rollout
- no folder-agent rollout across business directories in this feature
- no external channel / Telegram control-plane buildout
- no removal of coding-first priorities
- no broad domain expansion without explicit later plans

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- new generalized abstractions must prove they preserve current coding behavior rather than replacing it abstractly
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Agent OS Lift-In Contract And Substrate Boundary
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical lift-in boundary for generalizing coding-first VNX into a broader Agent OS substrate while preserving the coding core as authoritative.

### Scope
- define reusable manager/worker substrate responsibilities
- define what remains coding-specific vs what becomes general substrate
- define capability profile model for future domain-specific agent families
- define governance and authority boundaries that future domains must preserve
- define anti-goals that block premature broad rollout

### Deliverables
- Agent OS lift-in contract
- substrate boundary definition
- capability profile model
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- reusable substrate boundaries are explicit before implementation starts
- coding-first and generalized layers are clearly separated
- future domains can build on the substrate without reopening current core architecture
- broad rollout anti-goals are locked before generalization work lands

### Quality Gate
`gate_pr0_agent_os_lift_in_contract`:
- [ ] Contract defines reusable manager/worker substrate responsibilities
- [ ] Contract defines coding-specific vs generalized boundary explicitly
- [ ] Contract defines capability profile model for future domains
- [ ] Contract defines governance/authority boundaries and anti-goals for premature rollout
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Reusable Manager/Worker Substrate Extraction
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0]

### Description
Extract the reusable coding-first orchestration patterns into a stable substrate layer without changing current coding behavior.

### Scope
- extract reusable manager/worker abstractions from the coding runtime/orchestration layer
- preserve current coding flow behavior through compatibility tests
- expose stable seams for future domain-specific lift-in
- add tests for substrate extraction and compatibility preservation

### Deliverables
- reusable substrate extraction
- coding-compatibility preservation layer
- substrate compatibility tests
- GitHub PR with substrate extraction evidence summary

### Success Criteria
- stable reusable substrate exists without regressing coding flows
- future domains gain a real integration seam instead of a conceptual one
- coding-specific behavior remains authoritative where intended
- generalization work becomes incremental rather than architectural rework

### Quality Gate
`gate_pr1_substrate_extraction`:
- [ ] All substrate extraction and compatibility tests pass
- [ ] Reusable manager/worker abstractions are extracted without changing validated coding behavior under test
- [ ] Stable seams for future domain lift-in exist under test
- [ ] Coding-specific authority remains preserved under test
- [ ] GitHub PR exists with substrate extraction evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Capability Profiles And Domain Readiness Surfaces
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Add capability profiles and readiness surfaces so future domain-specific agents can declare what they support without overclaiming coding runtime guarantees.

### Scope
- implement capability profile model for manager, worker, session, runtime, and governance expectations
- expose readiness surfaces for future domain onboarding
- distinguish coding-authoritative profiles from future experimental profiles
- add tests for capability profile integrity and readiness-surface honesty

### Deliverables
- capability profile model
- domain readiness surface
- profile integrity tests
- GitHub PR with capability-profile evidence summary

### Success Criteria
- future domains can declare capabilities without pretending to inherit full coding maturity automatically
- coding vs experimental domain readiness remains explicit under test
- capability profiles make later Business OS planning less ambiguous
- substrate lift-in gains an honest onboarding surface

### Quality Gate
`gate_pr2_capability_profiles`:
- [ ] All capability profile and readiness-surface tests pass
- [ ] Coding-authoritative and experimental profiles are distinguishable under test
- [ ] Readiness surfaces remain explicit and honest under test
- [ ] Future domain onboarding uses stable capability semantics under test
- [ ] GitHub PR exists with capability-profile evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Future Domain Plan Scaffolding And Guardrail Integration
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @planner
**Requires-Model**: opus
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Create canonical scaffolding and guardrail integration for future non-coding and regulated plan families so they inherit the new substrate honestly without triggering premature rollout.

### Scope
- define template/scaffolding expectations for future business and regulated plan families
- integrate lift-in guardrails into future planning surfaces
- ensure future plans reference capability profiles and substrate boundaries explicitly
- add tests or validation checks for plan-scaffolding integrity where applicable

### Deliverables
- future domain plan scaffolding
- guardrail-integrated planning template expectations
- validation checks or documented integrity rules
- GitHub PR with planning-surface evidence summary

### Success Criteria
- future domain plans can be authored without reopening current architecture choices
- planning templates inherit substrate and guardrail requirements explicitly
- broad rollout remains governed instead of accidentally implied
- lift-in becomes a real planning bridge rather than a vague strategy statement

### Quality Gate
`gate_pr3_future_domain_scaffolding`:
- [ ] Future domain scaffolding references substrate boundaries and capability profiles explicitly
- [ ] Guardrail integration is present in the planning surface under test or validation
- [ ] Future plan scaffolding does not imply premature rollout under test/review
- [ ] GitHub PR exists with planning-surface evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Agent OS Lift-In Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify that the coding-first substrate is now generalized enough to support future Agent OS plan families without destabilizing the coding core or implying premature domain rollout.

### Scope
- certify substrate extraction and coding-compatibility preservation
- certify capability-profile honesty and future-domain readiness surfaces
- certify planning scaffolding and guardrail integration for future lift-in families
- certify that planning/status docs are updated for the new baseline

### Deliverables
- Agent OS lift-in certification report
- retained evidence for substrate compatibility and readiness surfaces
- updated internal planning docs (`CHANGELOG.md`, `PROJECT_STATUS.md`)
- GitHub PR with certification verdict

### Success Criteria
- coding-first substrate is stable enough to generalize without reopening core architecture
- future domain plan families have an honest lift-in path
- coding authority remains intact while the broader Agent OS bridge becomes concrete
- planning docs reflect the new post-Feature-19 baseline

### Quality Gate
`gate_pr4_agent_os_lift_in_certification`:
- [ ] Certification proves coding compatibility is preserved after substrate generalization
- [ ] Certification proves capability profiles and readiness surfaces are honest and usable
- [ ] Certification proves planning scaffolding and guardrail integration support future lift-in safely
- [ ] `docs/internal/plans/CHANGELOG.md` updated with Feature 19 closeout
- [ ] `docs/internal/plans/PROJECT_STATUS.md` updated with Feature 19 status and next-order recommendation
- [ ] GitHub PR exists with certification verdict
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
