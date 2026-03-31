# Feature: Deterministic Queue State Reconciliation

**Status**: Planned
**Priority**: P1
**Branch**: `feature/deterministic-queue-state-reconciliation`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Make VNX queue state deterministic from canonical runtime evidence so T0 stops relying on stale projections during active autonomous execution.

Execution context:
- first feature in the unattended 4-feature hardening chain
- no routine operator checkpoints
- T1 and T2 are Sonnet-pinned terminals
- branch-local review evidence is mandatory

Review gate policy:
- Gemini headless review is required on every PR in this feature
- Codex headless final gate is required on every PR in this feature because queue truth, promotion safety, and closure evidence are all chain-critical

## Problem Statement

The recent trial exposed a governance flaw:
- dispatch/runtime truth and queue projection truth drift apart
- T0 can see `In Progress: None` while a real dispatch is active
- closure and promotion decisions then depend on manual archaeology instead of deterministic evidence

This is not only a UI issue. It is a source-of-truth failure in an autonomous chain.

## Design Goal

Queue truth must be derived and refreshed in this order:

1. `FEATURE_PLAN.md`
   - valid PR ids and dependency graph
2. dispatch filesystem truth
   - `active`, `completed`, `pending`, `staging`, `rejected`
3. receipts and review evidence
   - supporting runtime evidence
4. queue projection files
   - cached views only, never primary truth
5. progress projections
   - advisory only

The system must detect, surface, and reconcile drift before promotion and before PR completion.

## Non-Goals

- no full queue-engine rewrite
- no speculative orchestration redesign
- no replacement of receipt processing
- no replacement of feature-plan driven dependencies
- no hidden fallback to stale projections

## Dependency Flow

```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
```

## PR-0: Queue Truth Contract And Source Hierarchy
**Track**: C
**Priority**: P1
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
Define the canonical source hierarchy for queue state so VNX stops trusting stale projections over runtime truth during active feature execution.

### Scope
- define source priority for:
  - `FEATURE_PLAN.md`
  - dispatch filesystem state
  - receipts
  - queue projections
  - progress projections
- define what counts as authoritative for:
  - completed
  - active
  - pending
  - blocked
- define when stale projection must be treated as mismatch instead of truth
- lock non-goals so this does not become a full queue-engine rewrite

### Success Criteria
- queue truth hierarchy is explicit
- projection drift is detectable and explainable
- reconciliation rules are deterministic
- T0 can distinguish source-of-truth state from cached projection

### Quality Gate
`gate_pr0_queue_truth_contract`:
- [ ] Contract defines source-of-truth priority among feature plan, dispatch files, receipts, and queue projections
- [ ] Contract defines deterministic rules for completed, active, pending, and blocked queue state
- [ ] Contract explains how projection drift is detected and surfaced
- [ ] Contract blocks silent reliance on stale queue projections during active execution
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-1: Reconcile Queue State From Canonical Runtime Evidence
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Implement deterministic queue reconciliation so queue status is rebuilt from canonical runtime evidence instead of drifting projections.

### Scope
- add reconciliation command/path for queue state
- derive status from:
  - active dispatches
  - completed dispatches
  - pending dispatches
  - receipts
  - feature-plan dependencies
- persist reconciled queue state with explicit provenance
- ensure `PR_QUEUE.md` is regenerated from reconciled truth
- add tests for stale projection and mid-run recovery scenarios

### Success Criteria
- reconciled queue state matches runtime truth under test
- mid-run projection drift can be repaired deterministically
- queue status includes provenance of why a PR is active, completed, pending, or blocked
- reconciliation is repeatable and idempotent

### Quality Gate
`gate_pr1_queue_reconciliation`:
- [ ] All queue reconciliation tests pass
- [ ] Reconciled queue state matches canonical dispatch and receipt state under test scenarios
- [ ] Projection drift can be repaired deterministically without manual file edits
- [ ] `PR_QUEUE.md` is regenerated from reconciled truth instead of stale cache
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-2: Kickoff, T0, And Per-PR Closure Integration
**Track**: C
**Priority**: P2
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1]

### Description
Integrate deterministic reconciliation into kickoff, T0 orchestration, and per-PR closure checks so queue truth and gate evidence truth are refreshed before promotion and before PR completion.

### Scope
- add reconcile-before-promote behavior to kickoff/T0 paths
- surface explicit drift warnings at pause checkpoints
- prevent stale queue views from being treated as truth during multi-feature runs
- add a per-PR closure-verifier mode so mid-chain PR certification is not forced through whole-feature closure logic
- detect contradictions between structured gate result payloads and normalized report content and fail explicitly when they disagree
- add tests for kickoff, promotion, and per-PR closure using stale or contradictory state

### Success Criteria
- T0 sees reconciled queue truth before promotion
- kickoff fails or warns explicitly when queue state is stale
- per-PR closure can be evaluated without pretending the whole feature is done
- contradictory gate JSON vs report content is surfaced as explicit evidence failure rather than silent ambiguity
- multi-feature progression no longer depends on manual queue archaeology
- drift findings remain operator-readable

### Quality Gate
`gate_pr2_kickoff_queue_integration`:
- [ ] All kickoff and T0 queue-integration tests pass
- [ ] Promotion path refreshes queue truth before acting on it
- [ ] Stale queue projection is surfaced explicitly at kickoff or checkpoint time
- [ ] Per-PR closure mode works without requiring whole-feature completion state
- [ ] Contradictory gate result JSON and report content fail with explicit evidence mismatch
- [ ] Multi-feature flow does not silently trust stale queue state under test
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## PR-3: Certification With Gemini Review And Codex Final Gate
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2]

### Description
Certify that deterministic reconciliation prevents the queue drift seen during the double-feature trial and produces auditable queue truth during real autonomous execution.

### Scope
- reproduce stale queue drift from the recent trial
- prove reconciliation restores correct state before next promotion
- capture operator evidence for source-of-truth vs projection
- require Gemini review and Codex final gate on certification

### Success Criteria
- trial-style queue drift no longer blocks or misleads progression
- certification evidence shows reconciled queue truth before further dispatch
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists for queue-core changes
- no chain-created open items remain unresolved at feature closure

### Quality Gate
`gate_pr3_queue_reconciliation_certification`:
- [ ] All queue reconciliation certification tests pass
- [ ] Reproduced queue drift is corrected before the next promotion in certification flow
- [ ] Certification evidence shows source-of-truth state and reconciled projection side by side
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings
- [ ] Feature closes with zero unresolved chain-created open items

## Test Plan

- unit tests
  - derive active PR from dispatch directories
  - derive completed PRs from dispatch directories and receipts
  - derive waiting and blocked PRs from dependency graph
  - ignore foreign or stale staging dispatches

- integration tests
  - active dispatch exists while projected state is stale -> reconcile fixes visible queue status
  - `PR_QUEUE.md` projection matches reconciled status summary
  - kickoff and promotion paths refresh queue truth before acting
  - per-PR closure succeeds only when gate evidence is present and internally consistent

- certification tests
  - reproduce the double-feature trial drift condition
  - verify reconciled truth before next promotion
  - verify Gemini and Codex evidence surfaces are branch-local and non-contradictory

- governance tests
  - T0 instructions mention queue reconciliation on mismatch
  - kickoff skill explicitly hands off to `@t0-orchestrator` after first safe promotion

## Expected Outcome

After this feature:
- queue status becomes meaningfully more trustworthy
- T0 does less manual inference
- active dispatch truth and visible queue state stop drifting apart silently
- the two-feature trial findings are folded back into a small, concrete hardening step
