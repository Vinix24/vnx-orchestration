# Feature: Failed Delivery Lease Cleanup And Runtime State Reconciliation

## PR-0: Delivery Failure And Lease Ownership Contract
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical contract for what must happen when a dispatch fails during terminal delivery so leases, claims, and runtime state do not silently strand a terminal in blocked state.

### Scope
- Define required cleanup behavior for:
  - tmux delivery failure
  - worker-side reject during execution handoff
  - Claude feedback or hook loop after context reset
  - stale pending-to-rejected transitions
- Define when canonical lease must be released
- Define how failed delivery differs from accepted execution
- Lock non-goals so this does not become a full broker/runtime rewrite

### Success Criteria
- Lease ownership rules are explicit for every delivery-failure path
- Failed delivery cannot leave a terminal silently blocked
- Cleanup obligations for dispatcher and runtime-core are deterministic
- The contract explains how runtime truth should reconcile after failure

### Quality Gate
`gate_pr0_failed_delivery_lease_contract`:
- [ ] Contract defines required lease and claim cleanup for every failed-delivery path
- [ ] Contract distinguishes failed delivery from accepted execution and worker failure
- [ ] Contract blocks silent terminal stranding after dispatch rejection or delivery failure
- [ ] Contract defines required audit evidence for cleanup and reconciliation

---

## PR-1: Release Canonical Lease On Delivery Failure
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Harden dispatcher failure handling so a dispatch that fails before real worker acceptance always releases the canonical lease and terminal claim.

### Scope
- Ensure every failed-delivery path releases canonical lease
- Ensure claim release and lease release stay paired
- Add tests for:
  - tmux transport failure
  - rejected execution handoff
  - Claude feedback or prompt-loop interruption after explicit clear-context
  - repeated retry followed by failure
- Emit explicit audit markers for release success or cleanup failure

### Success Criteria
- Failed delivery no longer strands a terminal lease
- Cleanup behavior is deterministic and auditable
- Retried delivery failures do not accumulate stale ownership
- Delivery-failure cleanup works even when subsequent bookkeeping partially fails

### Quality Gate
`gate_pr1_release_lease_on_failure`:
- [ ] All failed-delivery lease cleanup tests pass
- [ ] Canonical lease is released for every failed-delivery path before dispatch exits
- [ ] Terminal claim and canonical lease cleanup remain paired under test
- [ ] Cleanup failures are explicit in audit output rather than silent

---

## PR-2: Runtime Truth Reconciliation Between LeaseManager And Runtime Core
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0, PR-1]

### Description
Eliminate or explicitly reconcile divergent terminal state projections so LeaseManager and runtime_core_cli do not disagree about whether a terminal is idle, leased, or blocked.

### Scope
- Identify canonical source of truth for terminal ownership state
- Reconcile LeaseManager projection with runtime core broker state
- Reconcile PR queue/projected in-progress state with active dispatch/runtime truth so a live execution cannot appear as `In Progress: None`
- Add explicit mismatch detection and operator-readable diagnostics
- Add tests for divergent generation or state snapshots
- Add tests where:
  - active dispatch exists but queue/projected in-progress state is empty
  - terminal is visibly executing while queue projection still reports idle
  - reconciliation restores consistent operator-visible truth without duplicate dispatch

### Success Criteria
- Operators no longer see one subsystem report idle while another reports blocked
- Operators no longer see queue/projected state claim nothing is in progress while an active dispatch is already executing
- Runtime truth is derived from one canonical source or a deterministic reconciliation path
- Divergent state becomes explicit and repairable
- Dispatch safety checks use the same effective truth seen by operator tooling

### Quality Gate
`gate_pr2_runtime_state_reconciliation`:
- [ ] All runtime-state reconciliation tests pass
- [ ] LeaseManager and runtime-core state no longer diverge silently under test scenarios
- [ ] Queue/projected in-progress state reconciles correctly against active dispatch/runtime truth
- [ ] Divergent generation or state snapshots produce explicit diagnostics
- [ ] Dispatch safety checks consume reconciled runtime truth rather than conflicting projections

---

## PR-3: Dispatch Failure Classification And Operator Visibility
**Track**: C
**Priority**: P2
**Complexity**: Medium
**Risk**: Medium
**Skill**: @reviewer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1, PR-2]

### Description
Improve failure classification and operator surfaces so delivery failures explain whether the issue was invalid skill, stale lease, blocked runtime state, or worker-side handoff failure.

### Scope
- Add explicit failure reasons for:
  - invalid skill
  - stale lease
  - runtime state divergence
  - worker-side execution handoff failure
  - hook or feedback-loop interruption after terminal reset
- Surface cleanup outcome in operator-readable state
- Verify rejected dispatches preserve actionable reason text
- Add tests for diagnostic visibility

### Success Criteria
- Operators can distinguish configuration failure from runtime ownership failure
- Rejected dispatches retain actionable root-cause evidence
- Cleanup outcome is visible rather than inferred
- T0 can decide whether to retry, reroute, or escalate from explicit signals

### Quality Gate
`gate_pr3_failure_classification_visibility`:
- [ ] Failure classification tests pass for invalid skill, stale lease, runtime divergence, worker-side handoff failure, and hook/feedback-loop interruption
- [ ] Rejected dispatches preserve actionable root-cause markers
- [ ] Cleanup outcome is visible in operator-readable state or audit artifacts
- [ ] T0 can distinguish retryable from non-retryable delivery failures deterministically

---

## PR-4: Certification With Real Failed-Delivery Reproduction
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2, PR-3]

### Description
Certify the fix by reproducing a failed delivery and proving the terminal does not remain blocked, the runtime state stays consistent, and the next valid dispatch can proceed without manual lease surgery.

### Scope
- Reproduce at least one real failed-delivery path
- Verify lease cleanup and claim cleanup occur automatically
- Verify runtime truth remains consistent across operator tools
- Verify active dispatch state, queue/projected in-progress state, and terminal activity remain mutually consistent after failure and after recovery
- Require Gemini review and Codex final gate on certification and runtime-core PRs

### Success Criteria
- A failed dispatch no longer strands the target terminal
- The next valid dispatch can proceed without manual lease recovery
- Operator-visible state stays consistent after failure
- Operator-visible state does not regress to `In Progress: None` while a recovered or continuing dispatch is still active
- Gemini review and Codex final gate evidence exist for runtime-core changes

### Quality Gate
`gate_pr4_failed_delivery_certification`:
- [ ] All failed-delivery certification tests pass
- [ ] Reproduced failed delivery does not leave the target terminal blocked
- [ ] Lease cleanup and runtime-state reconciliation are both visible in certification evidence
- [ ] Certification proves queue/projected in-progress state matches active dispatch and terminal activity before and after recovery
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
