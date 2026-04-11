# Feature: Multi-Feature Autonomy Hardening And Chain Recovery

**Feature-ID**: Feature 14
**Status**: Complete
**Priority**: P1
**Branch**: `feature/multi-feature-autonomy-hardening-and-chain-recovery`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Make unattended multi-feature execution reliable enough that T0 can chain several features in sequence with deterministic resume, requeue, transition, and carry-forward behavior.

Execution context:
- direct follow-on after Feature 12 and Feature 13
- maps primarily to Roadmap M2: Multi-Feature Autonomy Hardening
- assumes runtime truth and first operator visibility already exist and now need to be exploited for longer unattended chains

Execution preconditions:
- both Gemini and Codex headless gates must be proven end-to-end on current `main` before this feature starts
- no provider-disabled or `not_executable` steady-state is acceptable for this feature family
- Feature 12 and Feature 13 must already be merged and reflected in the active baseline used for chain advancement

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because chain reliability is merge-critical
- no PR in this feature may proceed under provider-disabled waiver language for Gemini or Codex
- every PR in this feature must be opened as a GitHub PR before merge consideration
- no downstream PR may be promoted until the upstream PR is merged from green GitHub CI on updated `main`

## Problem Statement

Even with stronger runtime truth and dashboard visibility, unattended multi-feature execution can still break at chain boundaries:
- resume and requeue decisions can drift across features
- branch/worktree transitions can silently degrade if a new feature starts from the wrong baseline
- carry-forward findings can be lost or re-discovered instead of being promoted into the next feature
- chain recovery after a mid-run interruption can become ad hoc rather than governed

## Design Goal

Create a governed chain-execution model in which multi-feature runs can pause, resume, requeue, and advance deterministically while preserving worktree discipline, open-item continuity, and feature-by-feature evidence.

## Non-Goals

- no broad business-domain manager/worker rollout
- no full runtime transport rewrite
- no major dashboard redesign beyond the state already provided by Feature 13
- no attempt to solve general long-term memory or broad learning-loop intelligence in this feature

## Delivery Discipline

- each PR must have a GitHub PR with clear scope and linked feature name before merge
- required GitHub Actions checks must be green before human merge
- dependent PRs must branch from post-merge `main`, not from stale local branches
- no chain hardening PR may merge against ambiguous runtime semantics or unverified dashboard truth from Features 12 and 13
- final certification must update the internal planning progress docs in `docs/internal/plans/`

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
```

## PR-0: Multi-Feature Chain Contract And Recovery Policy
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical chain lifecycle for multi-feature execution: advancement conditions, resume and requeue policy, branch/worktree transition rules, and carry-forward evidence requirements.

### Scope
- define the chain state model from feature start through feature advancement and chain close
- define resume-safe vs resume-unsafe conditions between features
- define when a failed feature attempt is requeued vs blocked vs escalated
- define branch/worktree advancement rules from merged `main`
- define carry-forward rules for findings, open items, and residual risks

### Deliverables
- multi-feature chain execution contract
- chain recovery and requeue policy
- branch/worktree advancement rules
- GitHub PR with contract summary and acceptance notes

### Success Criteria
- chain progression semantics are explicit and finite
- resume and requeue decisions are governed instead of ad hoc
- branch/worktree transition discipline is locked before implementation
- carry-forward behavior is explicit and auditable

### Quality Gate
`gate_pr0_chain_contract_and_recovery_policy`:
- [ ] Contract defines chain states, advancement rules, and stop conditions
- [ ] Contract defines resume-safe and resume-unsafe conditions between features
- [ ] Contract defines deterministic requeue vs block vs escalation behavior
- [ ] Contract defines branch/worktree transition rules from merged `main`
- [ ] Contract defines carry-forward rules for findings, open items, and residual risk
- [ ] GitHub PR exists with feature-linked summary and acceptance notes
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Chain State Projection And Feature Advancement Truth
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-0]

### Description
Implement a canonical chain-state projection so T0 and operators can see which feature is active, what the next feature is, and whether the chain is truly safe to advance.

### Scope
- add chain state projection for current feature, next feature, blocked feature, and recovery-needed states
- derive advancement truth from merged feature state plus certification status
- expose carry-forward findings and unresolved chain items in the chain state surface
- add tests for advancement, blocked, and recovery-needed states

### Deliverables
- chain state projection layer
- feature advancement truth logic
- chain-state tests
- GitHub PR with state evidence summary

### Success Criteria
- chain progression truth is queryable from one stable surface
- feature advancement does not rely on implicit operator memory
- unresolved chain items are visible before the next feature starts
- tests cover blocked and recovery-needed paths

### Quality Gate
`gate_pr1_chain_state_projection`:
- [ ] All chain-state projection tests pass
- [ ] Current feature, next feature, blocked, and recovery-needed states are distinguishable under test
- [ ] Advancement truth requires merged certification state and does not rely on ad hoc operator memory
- [ ] Carry-forward findings and unresolved chain items are visible in the chain state surface
- [ ] GitHub PR exists with implementation and evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Resume, Requeue, And Branch/Worktree Transition Enforcement
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-1]

### Description
Implement chain-safe resume, requeue, and feature-transition enforcement so interrupted or failed chains recover deterministically instead of degenerating into duplicated or stale execution.

### Scope
- enforce resume-safe vs resume-unsafe decisions using canonical chain state
- enforce deterministic requeue policy for recoverable feature interruptions
- block chain advancement when next-feature branch/worktree is not derived from merged `main`
- preserve carry-forward feature context needed for the next feature dispatch
- add tests for interrupted chains, stale branch starts, and recovery requeue paths

### Deliverables
- chain resume and requeue enforcement
- branch/worktree transition guard
- carry-forward context handoff for next feature creation
- GitHub PR with recovery-path evidence summary

### Success Criteria
- interrupted chains can resume or requeue deterministically
- next-feature branch creation cannot silently drift from the merged baseline
- recoverable interruptions do not force full manual re-orchestration
- chain carry-forward remains intact across transitions

### Quality Gate
`gate_pr2_chain_resume_requeue_and_transition_enforcement`:
- [ ] All chain recovery and transition tests pass
- [ ] Resume-safe and resume-unsafe paths are enforced from canonical chain state under test
- [ ] Recoverable interruptions requeue deterministically instead of collapsing into manual ad hoc recovery
- [ ] Next-feature branch/worktree transition is blocked when not derived from merged `main`
- [ ] Carry-forward feature context persists into the next feature under test
- [ ] GitHub PR exists with recovery-path evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Chain-Level Findings Carry-Forward And Residual Governance
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-2]

### Description
Certify that chain-level findings, residual risks, and new open items survive across feature boundaries and remain visible until properly closed or deliberately carried forward.

### Scope
- verify carry-forward of findings from one feature into the next feature’s planning and closeout context
- verify unresolved chain-created open items remain visible and cumulative
- verify chain stop conditions remain explicit when blocking findings persist
- produce a chain-level residual governance model for final certification

### Deliverables
- chain carry-forward certification evidence
- cumulative open-item and residual-risk verification
- residual governance summary for multi-feature runs
- GitHub PR with chain-governance evidence summary

### Success Criteria
- chain findings do not disappear between features
- unresolved chain-created items remain visible until closed or explicitly deferred
- chain stop conditions remain operator-readable and auditable
- final certification can explain what was carried forward and why

### Quality Gate
`gate_pr3_chain_findings_carry_forward`:
- [ ] All chain carry-forward certification tests pass
- [ ] Findings persist across feature boundaries under test
- [ ] Unresolved chain-created open items remain cumulative and visible under test
- [ ] Blocking findings keep the chain in an explicit stop state under test
- [ ] GitHub PR exists with chain-governance evidence summary
- [ ] Required GitHub Actions checks are green before merge
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-4: Multi-Feature Autonomous Chain Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 3-6 hours
**Dependencies**: [PR-3]

### Description
Run and certify a governed unattended multi-feature chain using the new recovery, advancement, and carry-forward rules, proving that several features can execute in sequence without T0 babysitting every boundary.

### Scope
- execute a chained multi-feature run using the new chain-state and recovery rules
- verify advancement only occurs after merged green-CI feature completion
- verify interruption and recovery behavior under at least one recoverable disruption
- update `docs/internal/plans/CHANGELOG.md` with feature-closeout summary and next-step recommendation
- update `docs/internal/plans/PROJECT_STATUS.md` with the new chain-capable baseline and remaining next-order steps
- require Gemini review and Codex final gate on the certification PR

### Deliverables
- chain certification report
- evidence for advancement, interruption, recovery, and carry-forward behavior
- sequencing audit for GitHub PR progression and green-CI compliance
- updated internal planning changelog
- updated internal planning project status

### Success Criteria
- a multi-feature chain can advance with materially less operator babysitting
- recoverable chain interruption no longer destroys chain continuity
- carry-forward findings remain visible and cumulative across the certified run
- both Gemini and Codex gates execute successfully on the certified chain PRs
- this feature closes with zero unresolved chain-created open items

### Quality Gate
`gate_pr4_multi_feature_chain_certification`:
- [ ] All multi-feature chain certification tests pass
- [ ] Certified chain evidence shows advancement only after merged green-CI feature completion
- [ ] Recoverable chain interruption and recovery behavior are proven under test
- [ ] Carry-forward findings remain visible across the certified run
- [ ] Gemini and Codex both execute to terminal success on the certification path with request, result, and report artifacts present
- [ ] `docs/internal/plans/CHANGELOG.md` is updated with feature-closeout progress and next recommended order
- [ ] `docs/internal/plans/PROJECT_STATUS.md` is updated with the new chain-capable baseline
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
1. Close Feature 14 in the queue and confirm no unresolved blocker open items remain.
2. Perform chain-boundary runtime cleanup / stale-lease reconciliation.
3. Materialize Feature 15 into root `FEATURE_PLAN.md`.
4. Reinitialize `PR_QUEUE.md` from the new plan.
5. Run kickoff preflight for `PR-0`.
6. Promote exactly one kickoff dispatch for Feature 15.
7. Continue normal orchestration from the new queue state.

Next feature to start automatically:
- Feature 15: Context Injection And Handover Quality
