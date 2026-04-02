# Feature: Context Injection And Handover Quality

**Feature-ID**: Feature 15
**Status**: Planned
**Priority**: P1
**Branch**: `feature/context-injection-and-handover-quality`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Improve context injection, handover quality, and resume fidelity so autonomous coding runs receive the right amount of context, waste fewer tokens, and require fewer T0 redispatches.

Execution context:
- intended follow-on after Feature 14 chain hardening
- maps primarily to Roadmap M3: Intelligence And Context Injection Upgrade
- assumes runtime truth, operator visibility, and chain carry-forward are already available and can now feed better context selection

Execution preconditions:
- Feature 14 chain hardening baseline must be merged first
- both Gemini and Codex headless gates must remain operational on current `main` throughout this feature
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- interface-only Runtime Adapter work may overlap only if T0 explicitly documents that no M3 deliverable is forced to rework around it

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because prompt quality and resume reliability directly affect autonomous chain success
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

Autonomous coding still loses efficiency and coherence when context is too broad, too stale, or too weakly structured:
- workers can receive more history than they need and still miss the critical detail
- resumes and handovers can force T0 to redispatch because the next actor did not receive enough bounded context
- repeated failures and useful outcomes are not yet transformed into reusable context signals cleanly enough
- longer chains will remain noisy if context selection stays implicit and unmeasured

## Design Goal

Create a bounded, measurable context-injection and handover system that improves worker relevance, reduces prompt waste, and raises first-pass resume acceptance in autonomous coding flows.

## Non-Goals

- no broad local-LLM housekeeping layer in this feature
- no semantic-search platform rewrite
- no cross-domain business preference system rollout
- no runtime transport rearchitecture here

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- context-improvement claims must be backed by measurable evidence, not intuition
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Context Injection And Handover Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the bounded-context contract, handover structure, and measurable acceptance targets for context injection and resume quality.

### Scope
- define the canonical context bundle structure for autonomous coding dispatches
- define what belongs in mandatory context vs optional supporting context
- define handover and resume payload structure
- define measurable success criteria for context waste and resume acceptance
- define stale-context rejection rules

### Deliverables
- context injection contract
- handover and resume payload contract
- measurement contract for context waste and resume acceptance
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- context selection rules are explicit and bounded
- handover structure is standardized before implementation starts
- measurement targets are locked before optimization begins
- stale-context reuse becomes an explicit defect class

### Quality Gate
`gate_pr0_context_and_handover_contract`:
- [ ] Contract defines bounded context bundle structure for autonomous coding dispatches
- [ ] Contract defines mandatory vs optional context components
- [ ] Contract defines standardized handover and resume payload structure
- [ ] Contract defines measurable acceptance targets for context waste and resume quality
- [ ] Contract defines stale-context rejection rules
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Context Selection And Budget Enforcement
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-0]

### Description
Implement deterministic context selection and budget enforcement so autonomous dispatches include the right evidence, history, and carry-forward signals without bloating prompts.

### Scope
- implement bounded context assembly against explicit budget targets
- include high-value carry-forward evidence while excluding stale or irrelevant history
- enforce mandatory context components and rejection of stale-context inputs
- add tests for budget enforcement and stale-context rejection

### Deliverables
- bounded context assembly implementation
- context budget enforcement
- stale-context rejection checks
- GitHub PR with context-selection evidence summary

### Success Criteria
- context injection stays within explicit budget boundaries
- stale or irrelevant context is blocked from entering validated dispatch paths
- high-value carry-forward evidence remains present under test
- context selection becomes deterministic enough to review and certify

### Quality Gate
`gate_pr1_context_selection_and_budget_enforcement`:
- [ ] All context selection and budget tests pass
- [ ] Validated dispatch path enforces explicit context budget boundaries under test
- [ ] Stale-context inputs are rejected under test
- [ ] Carry-forward evidence remains included when required under test
- [ ] GitHub PR exists with context-selection evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Handover And Resume Payload Quality Enforcement
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-1]

### Description
Implement standardized handover and resume payload generation so downstream workers or T0 reviews receive enough structured context to continue without immediate redispatch.

### Scope
- implement standardized handover payload generation
- implement resume payload generation for interrupted or resumed work
- enforce required fields for status, next action, evidence, residual risks, and open items
- add tests for handover completeness and resume fidelity

### Deliverables
- handover payload generation
- resume payload generation
- completeness and fidelity tests
- GitHub PR with handover-quality evidence summary

### Success Criteria
- handovers are structured enough for downstream actors to continue coherently
- resumes contain enough actionable context to avoid avoidable redispatches
- required residual risks and open items survive into handovers under test
- handover quality becomes deterministic rather than stylistic

### Quality Gate
`gate_pr2_handover_and_resume_quality`:
- [ ] All handover and resume quality tests pass
- [ ] Standardized handover payload includes required status, next-action, evidence, residual-risk, and open-item fields under test
- [ ] Resume payload contains enough context to continue without immediate redispatch in validated scenarios under test
- [ ] Required residual risks and open items survive into handover and resume payloads under test
- [ ] GitHub PR exists with handover-quality evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Outcome Signals And Reusable Context Inputs
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @data-analyst
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Promote repeated outcomes, prior failures, and useful chain evidence into reusable context inputs so future dispatches and resumes draw from validated signals instead of undifferentiated history.

### Scope
- identify and surface reusable outcome signals from receipts, open items, and recent chain history
- distinguish reusable signals from stale narrative history
- expose reusable context inputs to the bounded context assembler
- add tests for reusable-signal inclusion and stale-history exclusion

### Deliverables
- reusable outcome-signal extraction
- reusable context input surface
- signal-selection tests
- GitHub PR with reusable-signal evidence summary

### Success Criteria
- useful repeated outcomes can be reused without dragging full old transcripts into the prompt
- stale narrative history is excluded where reusable structured signals exist
- context assembly quality improves through stronger inputs, not broader prompts
- learning-loop groundwork is improved without introducing heavy ML scope

### Quality Gate
`gate_pr3_reusable_context_inputs`:
- [ ] All reusable-signal tests pass
- [ ] Reusable outcome signals are available to the context assembler under test
- [ ] Stale narrative history is excluded when reusable structured signals exist under test
- [ ] Context assembly uses stronger reusable inputs without uncontrolled prompt growth under test
- [ ] GitHub PR exists with reusable-signal evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Context And Resume Quality Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Certify that bounded context injection and structured handovers materially improve autonomous coding flow quality, reduce prompt waste, and lower immediate redispatch rates.

### Scope
- measure bounded context waste on the validated dispatch path
- measure resume and handover acceptance on sampled autonomous coding scenarios
- verify stale-context rejection and reusable-signal inclusion in certification scenarios
- update `docs/internal/plans/CHANGELOG.md` with feature-closeout summary and next-step recommendation
- update `docs/internal/plans/PROJECT_STATUS.md` with the improved context-quality baseline and remaining next-order steps
- require Gemini review and Codex final gate on the certification PR

### Deliverables
- context and resume certification report
- measured evidence for context budget efficiency and resume acceptance
- stale-context and reusable-signal certification evidence
- updated internal planning changelog
- updated internal planning project status

### Success Criteria
- bounded context waste remains under the accepted threshold on the validated path
- resumes and handovers are accepted without immediate redispatch in the targeted share of sampled review cases
- context quality is measurably better, not just qualitatively preferred
- both Gemini and Codex gates execute successfully on the certification PR
- this feature closes with zero unresolved chain-created open items

### Quality Gate
`gate_pr4_context_and_resume_certification`:
- [ ] All context and resume certification tests pass
- [ ] Bounded context waste remains under 20 percent of total dispatch prompt budget on the validated path under test
- [ ] Resumes and handovers are accepted without immediate redispatch in at least 80 percent of sampled review cases
- [ ] Stale-context rejection and reusable-signal inclusion are proven in certification scenarios
- [ ] Gemini and Codex both execute to terminal success on the certification path with request, result, and report artifacts present
- [ ] `docs/internal/plans/CHANGELOG.md` is updated with feature-closeout progress and next recommended order
- [ ] `docs/internal/plans/PROJECT_STATUS.md` is updated with the new context-quality baseline
- [ ] Feature closes with zero unresolved chain-created open items
- [ ] GitHub PR exists with certification evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

### Post-Merge Continuation
If this certification PR merges cleanly and no feature-level blockers remain, T0 must
auto-continue into the next feature in the chain rather than waiting for a new human
kickoff prompt.

Required continuation sequence:
1. Close Feature 15 in the queue and confirm no unresolved blocker open items remain.
2. Perform chain-boundary runtime cleanup / stale-lease reconciliation.
3. Materialize Feature 16 into root `FEATURE_PLAN.md`.
4. Reinitialize `PR_QUEUE.md` from the new plan.
5. Run kickoff preflight for `PR-0`.
6. Promote exactly one kickoff dispatch for Feature 16.
7. Continue normal orchestration from the new queue state.

Next feature to start automatically:
- Feature 16: Runtime Adapter Formalization And Headless Transport Abstraction
