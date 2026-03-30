# Feature: Headless Run Observability Burn-In

**Status**: Complete
**Priority**: P1
**Branch**: `feature/headless-run-observability-burnin`
**Baseline**: FP-A through FP-D merged; adoption, packaging, and Pythonization feature merged on `main`
**Runtime policy**: Use this feature as an operational proving ground for mixed execution, not as a broad autonomy expansion
**Burn-in outcome**: Controlled rollout GO; small non-blocking follow-up items remain split out as separate work
**Burn-in fallout included here**: worktree path resolution and canonical intelligence sync hardening needed to keep standalone worktree runtime local and intelligence flow canonical

This feature is the recommended first burn-in feature after the adoption/productization work. It uses a small real feature to validate the newly hardened VNX runtime under realistic execution, while improving one of the most important unresolved areas: observability of headless runs.

Primary objective:
Make headless execution inspectable, classifiable, and governable enough for real operator use.

Secondary objective:
Use a small real feature to burn in starter mode, operator mode, mixed execution, receipts, provenance, recovery, and closure discipline together.

Estimated effort: ~5-8 engineering days across PR-0 through PR-4.

## Why This Is The Right Burn-In Feature

- It is small enough to be safe
- It touches real runtime behavior, not just docs
- It exercises mixed execution without forcing a full routing redesign
- It creates immediate operator value
- It exposes whether headless execution is truly ready or only unit-tested

## Design Principles

- observability before more autonomy
- operator clarity before more abstraction
- structured run state before richer dashboards
- minimal new control-plane surface, maximum signal
- prove headless runtime behavior before scaling it

## Governance Rules

| # | Rule | Rationale |
|---|------|-----------|
| G-R1 | **Headless runs must remain receipt-producing and provenance-linked** | No blind execution |
| G-R2 | **Operator must be able to inspect failed or hung runs without guesswork** | Recovery must be actionable |
| G-R3 | **No feature closure without real burn-in evidence** | This feature exists to prove operation |
| G-R4 | **No hidden retry or recovery behavior** | Headless control must stay explainable |
| G-R5 | **This feature must not weaken interactive tmux flows** | Headless hardening cannot regress operator mode |
| G-R6 | **No claimed test totals without real file and command verification** | Burn-in evidence must stay trustworthy |
| G-R7 | **No merge-ready claim without push, PR, CI, and truthful metadata** | Closure discipline from earlier features remains mandatory |
| G-R8 | **No pseudo-parallel dispatching onto the same terminal** | One terminal cannot provide true parallel execution for multiple active runs |

## Architecture Rules

| # | Rule | Description |
|---|------|-------------|
| A-R1 | **Every headless run gets a durable run identity** |
| A-R2 | **Run state must capture heartbeat and last-output timestamps** |
| A-R3 | **Logs must be persisted as artifacts, not just streamed to stdout** |
| A-R4 | **Exit outcomes must be classified, not just pass/fail** |
| A-R5 | **Process control must be group-aware where relevant** |
| A-R6 | **The first version can be simple, but not opaque** |
| A-R7 | **Interactive tmux flows must remain compatible with the new observability layer** |

## Source Of Truth

- headless run registry/state files or canonical runtime records
- run log artifacts
- receipts linked to runs
- operator recovery commands and inspection views
- burn-in certification report

## Known Failure Surface

1. Headless execution is currently more "possible" than fully operationally trusted
2. Process still alive does not mean useful output is still happening
3. Failing runs are harder to inspect than interactive terminal runs
4. Hung/no-output situations need better detection
5. Recovery actions need more precise signals than PID existence alone

## What MUST NOT Be Done

1. Do NOT introduce a giant new runtime substrate in this feature
2. Do NOT replace tmux or rewrite the whole execution model
3. Do NOT add broad new autonomy policies here
4. Do NOT add fake observability that cannot drive operator decisions
5. Do NOT close the feature on unit tests alone

## Dependency Flow

```text
PR-0 -> PR-1
PR-0 -> PR-2
PR-1, PR-2 -> PR-3
PR-3 -> PR-4
```

---

## PR-0: Headless Run Contract And Failure Taxonomy
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 0.5-1 day
**Dependencies**: []

### Description
Define what a headless run is in VNX terms, which state it must emit, and how failures are classified.

### Scope
- define headless run identity contract
- define required runtime fields
- define exit/failure classes
- define minimum operator inspection expectations
- define burn-in proof criteria

### Success Criteria
- headless run state model is explicit
- failure classes are concrete and usable
- later implementation PRs share one contract

### Quality Gate
`gate_pr0_headless_contract`:
- [ ] Headless run identity and lifecycle are defined clearly
- [ ] Failure taxonomy is actionable for recovery
- [ ] Minimum observability contract is explicit
- [ ] Burn-in proof criteria are measurable

---

## PR-1: Run Registry, Heartbeats, And Output Timestamps
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 1-2 days
**Dependencies**: [PR-0]

### Description
Add the core runtime data needed to observe headless runs while they are live.

### Scope
- add headless run registry/state representation
- persist run id, dispatch id, target, mode, pid/pgid where relevant
- capture `started_at`, `heartbeat_at`, and `last_output_at`
- expose result status and result class fields

### Success Criteria
- active headless runs become visible in structured state
- liveness is better than simple process existence
- recovery has better signals to work from

### Quality Gate
`gate_pr1_headless_registry`:
- [ ] Headless run identity is durable and inspectable
- [ ] Heartbeat and last-output timestamps are persisted
- [ ] Runtime state is sufficient for operator inspection
- [ ] Tests cover active, idle, and completed states

---

## PR-2: Structured Logs, Artifacts, And Exit Classification
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 1-2 days
**Dependencies**: [PR-0]

### Description
Persist useful output and classify results so failed headless runs stop looking like black boxes.

### Scope
- tee stdout/stderr to durable artifact files
- add log pointers into run state and/or receipts
- classify exit outcomes:
  - success
  - tool_failure
  - infra_failure
  - timeout
  - no_output_hang
  - interrupted

### Success Criteria
- operators can inspect a run after failure
- exit reason is more informative than exit code alone
- receipts and artifacts stay linked

### Quality Gate
`gate_pr2_logs_and_classification`:
- [ ] Headless run logs are persisted as artifacts
- [ ] Exit outcomes are classified consistently
- [ ] Receipts or linked state expose log pointers
- [ ] Tests cover multiple exit classes

---

## PR-3: Operator Inspection, Recovery Hooks, And Smoke Paths
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 1-2 days
**Dependencies**: [PR-1, PR-2]

### Description
Turn the new observability data into something operators can actually use during recovery and diagnosis.

### Scope
- add inspection command or view for headless runs
- add operator-facing summary for last output, status, and exit class
- connect headless state to recovery flow where appropriate
- add smoke scenarios for:
  - success
  - timeout
  - no-output hang
  - interrupted run

### Success Criteria
- operators can tell what happened without manual file spelunking
- recovery has stronger diagnostics
- the feature is useful during real burn-in, not just on paper

### Quality Gate
`gate_pr3_operator_inspection`:
- [ ] Operator can inspect active and failed headless runs cleanly
- [ ] Recovery paths use the new observability signals where relevant
- [ ] Smoke scenarios cover success, timeout, hang, and interrupt cases
- [ ] No regression is introduced to interactive operator mode

---

## PR-4: Burn-In Certification And Residual Risk Report
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 1 day
**Dependencies**: [PR-3]

### Description
Prove the feature in realistic use and document whether headless execution is operationally trustworthy enough for broader rollout.

### Scope
- run the feature through real starter/operator workflows
- validate receipts, provenance, and operator inspection paths
- validate at least one real headless task flow
- produce a certification report and residual risk summary

### Success Criteria
- burn-in evidence exists beyond unit tests
- residual headless risks are explicit
- next roadmap decision can be based on real evidence

### Quality Gate
`gate_pr4_burnin_certification`:
- [ ] Burn-in report includes real operator-run evidence
- [ ] At least one real headless task flow is validated end-to-end
- [ ] Receipts and provenance remain correct during headless runs
- [ ] Residual risks are explicit enough to guide the next cleanup or routing phase
