# Feature: Double-Feature Trial Certification

**Status**: Draft
**Priority**: P0
**Branch**: `feature/double-feature-trial-certification`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Prove that VNX can execute two small features in sequence with real governance, real branch transitions, real review-gate evidence, and correct worktree/session continuity.

Trial target sequence:
- Feature A: `Inline Stale Lease Reconciliation`
- Feature B: `Conversation Resume And Latest-First Timeline`

## Trial Invariants
- Feature B must not begin before Feature A is merged and independently closure-verified.
- Branch/worktree transition correctness is part of the trial, not an assumed external step.
- No pseudo-parallelism: one terminal may not carry two active dispatches at the same time.
- Headless review jobs must return structured receipts and normalized markdown reports, not only prose.
- Gemini review and Codex final gate must be exercised on real PRs where policy requires them.
- Interactive Claude control must use attach/jump/resume/recover semantics, not terminal injection assumptions.

## Dependency Flow
```text
PR-0 (no dependencies)
PR-1 (no dependencies)
PR-0, PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
PR-4 -> PR-5
```

## PR-0: Trial Contract, Evidence Model, And Invariants
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical trial contract for a real two-feature run so the test has hard pass/fail rules instead of vague “it seemed to work” interpretation.

### Scope
- define trial flow and invariants
- define required evidence for Feature A, transition, Feature B, and end-to-end certification
- define required branch/worktree checks and review-gate evidence
- lock non-goals so this does not become a new orchestration rewrite

### Success Criteria
- the trial contract explicitly defines what counts as success or failure
- required evidence is enumerated per trial stage
- branch transition correctness is part of the trial contract
- headless review receipts are required where policy says so

### Quality Gate
`gate_pr0_double_feature_trial_contract`:
- [ ] Contract defines pass/fail rules for Feature A, transition, Feature B, and final certification
- [ ] Contract defines branch/worktree transition correctness as explicit evidence, not assumption
- [ ] Contract defines required Gemini and Codex gate evidence for applicable PRs
- [ ] Contract blocks scope creep into a broad runtime redesign

---

## PR-1: Headless Review Job Contract And Evidence Receipts
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the contract for the headless review jobs used during the trial so Gemini review and Codex final gate produce structured evidence T0 can reason about.

### Scope
- define required receipt schema for headless review jobs
- define required normalized report path under `$VNX_DATA_DIR/unified_reports/`
- define pass, fail, blocked, advisory, and residual-risk fields
- define how review jobs bind to review contracts and PR ids
- define how T0 should treat missing or contradictory review evidence

### Success Criteria
- headless review jobs have a structured receipt contract
- headless review jobs have a deterministic normalized report contract
- Gemini and Codex results are usable as closure evidence
- missing or contradictory review evidence becomes explicit
- the contract fits the current review-contract architecture

### Quality Gate
`gate_pr1_headless_review_contract`:
- [ ] Headless review receipts define pass, fail, blocked, advisory findings, and residual risk explicitly
- [ ] Headless review results define a required report path under `$VNX_DATA_DIR/unified_reports/`
- [ ] Review receipts link deterministically to PR id and review contract id
- [ ] Missing or contradictory review evidence is explicitly representable
- [ ] Contract aligns with current review-contract and closure-verifier architecture

---

## PR-2: Feature A Trial Execution And Certification
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-0, PR-1]

### Description
Execute and certify Feature A (`Inline Stale Lease Reconciliation`) under the new trial contract, including required review-gate evidence and closure verification.

### Scope
- activate Feature A plan
- verify dispatch flow, lease behavior, and resulting evidence
- require Gemini review and Codex final gate on applicable PRs
- capture closure evidence and merge-readiness verdict for Feature A

### Success Criteria
- Feature A completes with required tests, review receipts, and closure evidence
- stale lease behavior is exercised as intended for the trial
- closure verification is explicit and auditable
- residual risks for Feature A are documented before transition

### Quality Gate
`gate_pr2_feature_a_trial_certification`:
- [ ] All Feature A required tests pass
- [ ] Gemini review receipt exists and all blocking findings are closed where policy requires review
- [ ] Codex final gate receipt exists and all required checks pass where policy requires final gate
- [ ] Required headless review reports exist at the report paths referenced by the gate results
- [ ] Feature A closure evidence and residual risk note are complete before transition

---

## PR-3: Branch, Worktree, And Auto-Next Transition Validation
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-4 hours
**Dependencies**: [PR-2]

### Description
Validate the transition between Feature A and Feature B so the next feature is loaded only after correct branch/worktree state, merge verification, and closure gates are satisfied.

### Scope
- verify Feature A merged-to-main state
- verify next-feature materialization behavior
- verify branch/worktree transition correctness
- verify no stale queue, stale lease, or stale session state leaks into Feature B start

### Success Criteria
- Feature B does not start on Feature A branch state
- the transition has explicit evidence for merge, branch, worktree, and queue correctness
- stale runtime state does not silently poison the next feature
- transition failures are explicit and operator-readable

### Quality Gate
`gate_pr3_transition_validation`:
- [ ] Feature A merge to main is independently verified before Feature B starts
- [ ] Branch/worktree transition correctness is proven with explicit evidence
- [ ] Queue, lease, and session state are clean or explicitly reconciled before Feature B activation
- [ ] Transition failures are explicit and operator-readable

---

## PR-4: Feature B Trial Execution And Certification
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 3-5 hours
**Dependencies**: [PR-3]

### Description
Execute and certify Feature B (`Conversation Resume And Latest-First Timeline`) under the same governance conditions after the validated transition.

### Scope
- activate Feature B plan after the validated transition
- verify conversation resume, latest-first behavior, and worktree/session linkage
- require Gemini review and Codex final gate on applicable PRs
- capture closure evidence and residual risk for Feature B

### Success Criteria
- Feature B completes with required tests, review receipts, and closure evidence
- session continuity and operator-facing retrieval value are demonstrated
- the feature is verified under post-transition conditions, not a fresh synthetic start
- residual risks for Feature B are explicit

### Quality Gate
`gate_pr4_feature_b_trial_certification`:
- [ ] All Feature B required tests pass
- [ ] Gemini review receipt exists and all blocking findings are closed where policy requires review
- [ ] Codex final gate receipt exists and all required checks pass where policy requires final gate
- [ ] Required headless review reports exist at the report paths referenced by the gate results
- [ ] Feature B closure evidence and residual risk note are complete after the validated transition

---

## PR-5: End-To-End Double-Feature Certification And Rollout Verdict
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-4]

### Description
Produce the end-to-end certification verdict for the first real double-feature trial and state clearly whether the system is ready for broader multi-feature use.

### Scope
- synthesize Feature A, transition, and Feature B evidence
- summarize review-gate behavior and operator friction
- classify remaining blockers vs warns vs deferred work
- state rollout recommendation for broader multi-feature execution

### Success Criteria
- the system receives a clear go/no-go verdict for broader multi-feature use
- operator friction and headless review behavior are explicitly assessed
- remaining blockers are not hidden inside generic residual risk text
- the certification is strong enough to guide the next roadmap step

### Quality Gate
`gate_pr5_end_to_end_double_feature_certification`:
- [ ] Final certification gives a clear go or no-go verdict for broader multi-feature use
- [ ] Final certification includes review-gate behavior, operator friction, and transition quality assessment
- [ ] Remaining blockers are separated from warnings and deferred work explicitly
- [ ] End-to-end certification evidence is complete and auditable
