# Feature: VNX Runtime Recovery, tmux Hardening, And Operability

**Status**: Complete
**Priority**: P1
**Branch**: `feature/runtime-recovery-tmux-operability-hardening`
**Baseline**: FP-A merged on `main` via PR #41; canonical runtime coordination, broker shadow path, lease manager, tmux adapter, reconciliation, and cutover compatibility are available
**Runtime policy**: T0 on Claude Opus; straightforward implementation PRs default to Sonnet; recovery-policy, escalation, and final operator-flow PRs require Opus; tmux remains the operator shell and execution host

This feature hardens the runtime around the new FP-A core. FP-A made dispatch durability, lease ownership, and transport state explicit. FP-B now makes that runtime survivable and usable under long-running operator conditions: failures must be classified, retries must be bounded, terminals must be recoverable without corrupting dispatch state, and operator commands must tell the truth about system health.

Primary objective:
Introduce workflow-aware supervision, a hybrid tmux runtime model, and production-grade operator commands so VNX can run for long sessions without fragile restart habits, hidden failure loops, or pane-state confusion.

Secondary objective:
Keep tmux as the operator shell while demoting it further from implicit state machine to recoverable host surface, then make `vnx doctor` and `vnx recover` strong enough to support FP-C and FP-D safely.

Estimated effort: ~10-14 engineering days across PR-0 through PR-5.

## Design Principles
- Preserve the FP-A control-plane truth; supervision and tmux must consume canonical runtime state, not replace it
- Keep tmux because operator visibility matters, but stop treating pane layout as workflow truth
- Separate process incidents from workflow incidents
- Bound all retry behavior with explicit budgets, cooldowns, and escalation
- Prefer declarative session profiles and recovery contracts over shell heuristics
- Make operator commands evidence-producing, not cosmetic wrappers

## Governance Rules

| # | Rule | Rationale |
|---|------|-----------|
| G-R1 | **No automatic recovery may hide a failure class** | T0 must be able to distinguish crash, timeout, delivery, and lease incidents |
| G-R2 | **Retry budgets are mandatory** — no infinite restart or resend loops | Prevents silent failure storms |
| G-R3 | **Every recovery action must emit an incident trail** | Recovery must be reviewable after the fact |
| G-R4 | **tmux layout changes cannot redefine terminal identity** | Pane drift must not corrupt runtime ownership |
| G-R5 | **Dead-letter is explicit** — dispatches that cannot safely resume must stop in a reviewable terminal state | Prevents false progress |
| G-R6 | **`vnx doctor` and `vnx recover` must operate on canonical runtime state** | Operator tools cannot lie by reading stale projections only |
| G-R7 | **Operator teardown and runtime supervision remain distinct** | Avoids mixing emergency cleanup with steady-state automation |
| G-R8 | **Final recovery authority remains governance-aware** — auto-retry may act within policy, but escalations remain explicit | Preserves governance-first behavior |

## Architecture Rules

| # | Rule | Description |
|---|------|-------------|
| A-R1 | **Process supervision and workflow supervision are separate layers** |
| A-R2 | **Incident records are durable and typed** — `process_crash`, `delivery_failure`, `ack_timeout`, `terminal_unresponsive`, `lease_conflict`, `resume_failed`, `repeated_failure_loop` |
| A-R3 | **Retry policy is persisted** — attempts, cooldown windows, and escalation state are durable |
| A-R4 | **tmux session profiles are declarative** — home layout, ops windows, and recovery windows derive from config, not ad-hoc shell logic |
| A-R5 | **Pane IDs are derived state** — terminal identity stays canonical even when tmux remaps |
| A-R6 | **`vnx doctor` is a preflight and integrity command, not a best-effort ping** |
| A-R7 | **`vnx recover` must reconcile leases, incidents, and tmux bindings before resuming work** |
| A-R8 | **Recovery commands must be idempotent** — repeated runs cannot compound the same incident or remap repeatedly |
| A-R9 | **Legacy bash supervisor paths stay available until FP-B cutover is certified** |
| A-R10 | **No new operability shortcut may bypass runtime evidence** |

## Source Of Truth
- Runtime coordination: SQLite runtime tables introduced in FP-A
- Incident log: new durable runtime incident store under `.vnx-data/state/`
- Retry budgets and escalation state: canonical runtime state
- tmux session profiles: declarative config under VNX system paths
- tmux adapter state and pane mappings: derived projection only
- Operator diagnostics: `vnx doctor` output plus runtime-backed health summaries
- Recovery actions: runtime events plus incident log, not shell-only side effects

## Known Failure Surface (Evidence / Problem Statement)
1. **Supervisor is still too process-oriented**: it can restart processes, but cannot reason about dispatch state, repeated loops, or dead-letter outcomes
2. **tmux layout remains semi-implicit runtime logic**: pane recovery and remap still risk being confused with worker identity
3. **Recovery policy is under-modeled**: crash, timeout, delivery failure, and lease conflict need different actions
4. **Operator commands are not strong enough yet**: `doctor` and `recover` need to validate runtime truth, not just presence checks
5. **Long-running operation remains fragile**: bounded autonomy cannot expand while restart and recovery semantics are not explicit
6. **Failure visibility is too noisy in the wrong places and too quiet in the important places**: operators need incident summaries, not hidden shell churn

## What MUST NOT Be Done
1. Do NOT remove tmux in this feature
2. Do NOT re-centralize state back into pane IDs or shell locks
3. Do NOT let `vnx doctor` become a shallow environment checklist only
4. Do NOT implement auto-retry without durable budgets and escalation state
5. Do NOT allow `vnx recover` to mutate runtime state without incident records
6. Do NOT merge workflow recovery and process teardown into one blind command
7. Do NOT start FP-C execution-mode expansion before FP-B recovery certification is green

## Dependency Flow
```text
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-3
PR-1, PR-2, PR-3 -> PR-4
PR-2, PR-3, PR-4 -> PR-5
```

---

## PR-0: Incident Taxonomy, Recovery Contracts, And Certification Matrix
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 1-2 days
**Dependencies**: []

### Description
Lock down the recovery language before implementation spreads. FP-B needs one incident taxonomy, one set of recovery contracts, and one certification matrix so later PRs do not invent incompatible semantics.

### Scope
- Define canonical incident classes and severity levels
- Define recovery contracts for `process_crash`, `delivery_failure`, `ack_timeout`, `terminal_unresponsive`, `lease_conflict`, `resume_failed`, and `repeated_failure_loop`
- Define bounded retry, cooldown, and escalation rules per incident class
- Define dead-letter entry rules and orchestrator escalation triggers
- Define tmux/runtime identity invariants for remap and reheal flows
- Create an FP-B certification matrix tying incident classes to expected recovery outcomes

### Success Criteria
- Incident classes and recovery actions are explicit and non-overlapping
- Retry and escalation rules are specified before supervisor changes land
- tmux identity rules are documented in a way later PRs can enforce
- FP-B has one certification matrix covering supervision, tmux, and operability flows

### Quality Gate
`gate_pr0_recovery_contracts`:
- [ ] Canonical incident taxonomy exists and is unambiguous
- [ ] Recovery contracts include retry, cooldown, escalation, and dead-letter rules
- [ ] tmux identity invariants are defined separately from pane mapping
- [ ] Certification matrix covers all in-scope incident classes
- [ ] Documentation and touched code paths pass syntax/import validation

---

## PR-1: Durable Incident Log, Retry Budgets, And Cooldown Shadow Path
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Add the durable incident substrate and bounded retry bookkeeping first, in shadow mode, so the existing supervisor can start recording the right truth before restart authority moves.

### Scope
- Add durable incident log schema and Python helpers
- Persist retry budgets, cooldown windows, and escalation counters
- Mirror current supervisor outcomes into typed incident records
- Add runtime-backed budget checks for dispatch and terminal recovery attempts
- Export operator-readable incident summaries
- Keep legacy supervisor behavior active while the new incident path shadows it

### Success Criteria
- Incident creation is durable and tied to dispatch, terminal, and component context
- Retry budgets and cooldowns are queryable from canonical state
- Repeated failure loops become measurable instead of inferred from logs
- Shadow mode can observe existing restart behavior without changing outcomes yet

### Quality Gate
`gate_pr1_incident_log_and_retry_budgets`:
- [ ] Durable incident records are written for process and workflow incidents
- [ ] Retry budgets and cooldown windows persist across process restarts
- [ ] Incident summaries can be generated without parsing shell logs
- [ ] Shadow mode does not change current recovery behavior by default
- [ ] Tests cover repeated-failure detection, budget decrement, and cooldown gating

---

## PR-2: Workflow Supervisor, Dead-Letter Routing, And Escalation Semantics
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Introduce workflow-aware supervision that can reason about dispatch state, incident class, retry budget, and dead-letter transitions without pretending every failure is just a crashed process.

### Scope
- Add workflow supervisor service or module layered on top of FP-A coordination state
- Route unrecoverable or budget-exhausted dispatches into explicit `dead_letter` or equivalent recoverable terminal state
- Implement escalation state transitions for T0 review
- Add checkpoint/resume-aware recovery hooks where dispatch state allows safe continuation
- Separate process restart decisions from workflow resume decisions
- Keep compatibility with the existing simple supervisor during transition

### Success Criteria
- Process crashes and workflow failures are handled by different logic paths
- Budget exhaustion stops loops and yields explicit dead-letter or escalation state
- Recovery outcomes are durable and reviewable
- T0-facing escalation triggers are explicit instead of hidden in restart noise

### Quality Gate
`gate_pr2_workflow_supervisor`:
- [ ] Workflow supervisor differentiates incident classes before choosing recovery actions
- [ ] Dead-letter and escalation transitions are explicit and durable
- [ ] Budget exhaustion prevents repeated blind retries
- [ ] Resume paths require compatible dispatch state and do not fabricate progress
- [ ] Tests cover dead-letter routing, escalation, and loop termination behavior

---

## PR-3: Declarative tmux Session Profiles, Remap, And Operator Shell Hardening
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Harden tmux as an operator shell instead of a hidden state machine: define session profiles, keep a stable home layout, and make remap/reheal use canonical identity rather than pane folklore.

### Scope
- Add declarative tmux session profile model for home, ops, recovery, and events views
- Refactor tmux bootstrap and remap paths to consume canonical terminal identity
- Add adapter-backed remap and reheal helpers that tolerate pane churn
- Preserve a stable T0/T1/T2/T3 home window while allowing dynamic ops/recovery windows
- Improve operator attach/switch commands around the new tmux profile model
- Keep legacy full-grid behavior available during transition if needed

### Success Criteria
- Pane remap no longer threatens dispatch or lease truth
- Operators keep a usable home layout while gaining clearer recovery/ops surfaces
- tmux lifecycle is declarative enough to be checked and rebuilt
- Session/profile recovery no longer depends on brittle hard-coded pane assumptions

### Quality Gate
`gate_pr3_tmux_runtime_model`:
- [ ] tmux session profiles define home and dynamic windows declaratively
- [ ] Terminal identity remains stable when pane IDs change
- [ ] Remap and reheal commands use canonical runtime state
- [ ] Home layout remains intact for operators during normal operation
- [ ] Tests cover remap behavior, profile regeneration, and adapter fallback logic

---

## PR-4: `vnx doctor` Hardening And Recovery Preflight
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 1-2 days
**Dependencies**: [PR-1, PR-2, PR-3]

### Description
Turn `vnx doctor` into a real operator integrity command that validates runtime health, incident pressure, tmux/session coherence, and recovery readiness from canonical state.

### Scope
- Expand `vnx doctor` to validate runtime schema status, lease health, queue health, incident pressure, and tmux profile consistency
- Add clear fail/warn/pass categories for operator use
- Surface stale or conflicting runtime projections versus canonical state
- Add recovery preflight output for `vnx recover`
- Document operator runbook expectations for doctor output
- Ensure doctor can run safely in dirty runtime conditions without mutating state

### Success Criteria
- `vnx doctor` detects real recovery blockers before an operator invokes `recover`
- Doctor output maps directly to runtime evidence and incident state
- Operators can distinguish environmental issues from dispatch/workflow issues
- `doctor` becomes the standard preflight for long-running sessions and recoveries

### Quality Gate
`gate_pr4_doctor_hardening`:
- [ ] `vnx doctor` reads canonical runtime state, not only projections
- [ ] Output distinguishes pass, warn, and fail conditions with concrete reasons
- [ ] Recovery preflight identifies blockers for remap, lease reconciliation, or incident overload
- [ ] Doctor is read-only and idempotent
- [ ] Tests cover healthy, degraded, and blocked runtime scenarios

---

## PR-5: `vnx recover` Operator Flow, Cutover, And FP-B Certification
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-2, PR-3, PR-4]

### Description
Cut over to the new operator recovery path only after the incident model, workflow supervisor, tmux hardening, and doctor preflight are all proven. `vnx recover` becomes the bounded, governance-compatible recovery entry point for the runtime.

### Scope
- Implement the operator-facing `vnx recover` flow against canonical state
- Reconcile leases, incidents, and tmux bindings before any resume attempt
- Produce incident summary and recommended actions for operators/T0
- Move recovery authority from legacy ad-hoc paths into the certified FP-B path
- Add rollback and cutover checks for the new supervision/recovery stack
- Certify FP-B against the PR-0 matrix and document residual risks

### Success Criteria
- `vnx recover` can safely restore operator control without fabricating worker state
- Recovery cutover is bounded, reviewable, and reversible
- Operators can see exactly what was reconciled, escalated, or deferred
- FP-B ends with a certified recovery path that unblocks FP-C

### Quality Gate
`gate_pr5_recover_cutover_and_certification`:
- [ ] `vnx recover` reconciles leases, incidents, and tmux bindings before resume
- [ ] Recovery output includes explicit summary, escalation items, and remaining blockers
- [ ] Legacy recovery shortcuts are either removed from normal use or clearly marked as fallback-only
- [ ] Cutover has rollback guidance and certification evidence
- [ ] Full FP-B verification passes before feature closure

