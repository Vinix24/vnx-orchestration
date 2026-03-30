# Feature: VNX Roadmap Autopilot, Auto-Next Feature Loading, And Multi-Reviewer Gates

**Status**: Draft
**Priority**: P0
**Branch**: `feature/roadmap-autopilot-review-gates`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Enable multi-feature roadmap orchestration with automatic feature handoff after merged + verified closure.

## Dependency Flow
```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-0 -> PR-2
PR-1, PR-2 -> PR-3
```

## PR-0: Roadmap Registry And Materialization
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Introduce the roadmap registry, active feature materialization, and roadmap state tracking.

### Scope
- roadmap registry
- active feature materialization
- roadmap state file

### Success Criteria
- roadmap can initialize and load one active feature
- root FEATURE_PLAN.md and PR_QUEUE.md represent only the active feature

### Quality Gate
`gate_pr0_roadmap_registry`:
- [ ] Roadmap registry initializes cleanly
- [ ] Feature materialization is deterministic

## PR-1: Review Gate Stack
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Add Gemini, Codex, and optional Claude GitHub review gate adapters.

### Scope
- review request manager
- review result persistence
- governance receipts for review events

### Success Criteria
- review requests and results are tracked durably
- optional Claude GitHub path skips cleanly when not configured

### Quality Gate
`gate_pr1_review_stack`:
- [ ] Review requests are emitted deterministically
- [ ] Result recording produces durable evidence

## PR-2: Closure Verifier And Auto-Merge Policy
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Add executable closure verification and conditional auto-merge policy evaluation.

### Scope
- closure verifier
- metadata sync checks
- GitHub merge-state verification
- conditional auto-merge evaluator

### Success Criteria
- T0 closure-ready claims become executable
- low-risk conditional auto-merge is policy-evaluable

### Quality Gate
`gate_pr2_closure_policy`:
- [ ] Closure verifier blocks inconsistent merge claims
- [ ] Auto-merge policy blocks high-risk paths

## PR-3: Auto-Advance And Drift Fix-up Insertion
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-1, PR-2]

### Description
Advance the roadmap only after merged + verified closure, and insert blocking fix-up features when drift is detected.

### Scope
- roadmap reconcile
- roadmap advance
- fix-up feature insertion

### Success Criteria
- next feature loads automatically only after verified merge
- blocking drift inserts a fix-up before roadmap advancement

### Quality Gate
`gate_pr3_auto_advance`:
- [ ] Auto-next only occurs after merged + verified closure
- [ ] Blocking drift produces a fix-up feature instead of silent advancement
