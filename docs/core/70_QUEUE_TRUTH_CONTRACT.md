# Queue Truth Contract

**Status**: Canonical
**Feature**: Deterministic Queue State Reconciliation
**PR**: PR-0
**Gate**: `gate_pr0_queue_truth_contract`
**Date**: 2026-03-31
**Author**: T3 (Track C Architecture)

This document is the single source of truth for how VNX derives queue state. All downstream PRs (PR-1 through PR-3) implement against this contract. Any component that reads or writes queue state must conform to the hierarchy and rules defined here.

---

## 1. Why This Exists

### 1.1 The Problem

The double-feature trial exposed a governance flaw: dispatch runtime truth and queue projection truth drift apart during active execution. Concretely:

- T0 sees `In Progress: None` while a real dispatch is active in `.vnx-data/dispatches/active/`.
- PR_QUEUE.md shows a PR as "Queued" when dispatches for it have already completed.
- Closure and promotion decisions depend on manual archaeology instead of deterministic evidence.

This is not a display bug. It is a **source-of-truth failure** in an autonomous execution chain. When T0 trusts a stale projection over filesystem reality, it can:
- Skip a PR that is already active.
- Re-dispatch work that is already completed.
- Promote a PR whose gate evidence does not yet exist.
- Block on a dependency that is already satisfied.

### 1.2 The Fix

Queue state must be **derived** from canonical runtime evidence, not projected from cached snapshots. This contract defines:
1. Which sources are authoritative, and in what priority order.
2. What constitutes each queue state (completed, active, pending, blocked).
3. How projection drift is detected and surfaced.
4. When a stale projection must be treated as a mismatch rather than trusted.

---

## 2. Source-Of-Truth Hierarchy

Queue state is derived from five sources. They are listed in strict priority order. When sources disagree, the higher-priority source wins.

### 2.1 Priority Table

| Priority | Source | Role | Authoritative For |
|----------|--------|------|-------------------|
| **1** | `FEATURE_PLAN.md` | Structure definition | Valid PR IDs, dependency graph, track assignments, gate names, review-stack requirements |
| **2** | Dispatch filesystem state | Runtime execution truth | Which PRs are active, completed, pending, staging, or rejected right now |
| **3** | Receipts and review evidence | Completion evidence | Whether a dispatch actually finished, gate pass/fail status, evidence chain integrity |
| **4** | Queue projection files | Cached view | Pre-computed queue snapshots for T0 consumption (`pr_queue_state.json`, `pr_queue.json`, `PR_QUEUE.md`) |
| **5** | Progress projections | Advisory | Track-level gate progression, terminal status (`progress_state.yaml`, `t0_brief.json`) |

### 2.2 Priority Rules

**Rule P-1**: FEATURE_PLAN.md defines what exists.
If a PR ID does not appear in FEATURE_PLAN.md, it is not a valid queue entry regardless of what dispatch files or projections claim. FEATURE_PLAN.md is the structural authority: it defines the set of PRs, their dependency edges, and their governance metadata. It does not define runtime status.

**Rule P-2**: Dispatch filesystem defines what is happening.
The directories under `.vnx-data/dispatches/` (`active/`, `completed/`, `pending/`, `staging/`, `rejected/`) are the runtime authority for execution state. A dispatch file physically present in `active/` means that PR is active, regardless of what `pr_queue_state.json` claims. Filesystem state is checked by scanning directory contents, not by reading cached state files.

**Rule P-3**: Receipts confirm what happened.
A dispatch in `completed/` is not fully confirmed until a receipt with a terminal event (`task_complete`, `task_finished`, `done`) exists in the receipt trail for that dispatch ID. Receipts also carry gate evidence linkage. Completion without receipt is treated as **unconfirmed completion** — sufficient to unblock dependents but flagged for operator attention.

**Rule P-4**: Projections are never primary truth.
`pr_queue_state.json`, `pr_queue.json`, `pr_queue_state.yaml`, and `PR_QUEUE.md` are cached views. They are useful for quick T0 reads but must never be the sole basis for promotion, closure, or dispatch decisions during active execution. They are generated artifacts, not source data.

**Rule P-5**: Progress projections are advisory only.
`progress_state.yaml` and `t0_brief.json` reflect track-level progression and terminal health. They inform T0 about which gate a track has reached but do not override dispatch-level truth about individual PR status.

---

## 3. State Derivation Rules

Each PR in the queue is in exactly one of four states. The state is derived deterministically from the source hierarchy.

### 3.1 State Definitions

| State | Definition | Derived From |
|-------|-----------|--------------|
| **completed** | All dispatches for this PR are in `completed/` or `rejected/` and at least one dispatch completed successfully | Dispatch filesystem (Priority 2), confirmed by receipts (Priority 3) |
| **active** | At least one dispatch for this PR exists in `active/` | Dispatch filesystem (Priority 2) |
| **pending** | All dependencies are satisfied (completed), no dispatch exists in `active/` or `pending/` or `staging/`, and the PR is eligible for dispatch | FEATURE_PLAN.md dependency graph (Priority 1) + dispatch filesystem (Priority 2) |
| **blocked** | At least one dependency PR is not yet completed | FEATURE_PLAN.md dependency graph (Priority 1) + derived state of dependency PRs |

### 3.2 Derivation Algorithm

For each PR defined in FEATURE_PLAN.md, derive its state in this order:

```
1. Scan dispatch directories for any dispatch referencing this PR ID.

2. IF any dispatch for this PR exists in active/:
     state = active
     STOP

3. IF any dispatch for this PR exists in completed/ with a successful outcome:
     state = completed
     STOP

4. IF all dependency PRs (from FEATURE_PLAN.md) are in state "completed":
     IF any dispatch for this PR exists in pending/ or staging/:
       state = pending (dispatch is queued but not yet claimed)
     ELSE:
       state = pending (eligible for dispatch creation)
     STOP

5. ELSE:
     state = blocked (one or more dependencies are not completed)
     STOP
```

### 3.3 Dispatch-To-PR Binding

A dispatch is bound to a PR by the `PR-ID` field in the dispatch metadata block. The dispatch filename format `YYYYMMDD-HHMMSS-{pr-descriptor}-{track}` also encodes the PR context, but the metadata field is authoritative.

Multiple dispatches may exist for the same PR (e.g., a rejected dispatch followed by a re-dispatch). The derivation algorithm considers all dispatches for a PR, not just the most recent one.

### 3.4 Edge Cases

**EC-1: Dispatch in active/ but no heartbeat or receipt for extended period.**
State remains `active`. Staleness of the active dispatch is a separate concern (lease expiry, runtime reconciliation) handled by the runtime coordination layer. The queue truth contract does not reclassify an active dispatch as failed — that is a runtime decision, not a queue decision.

**EC-2: Dispatch in completed/ but no receipt.**
State is `completed` (unconfirmed). The PR is treated as completed for dependency resolution, but a drift warning is raised. The receipt gap must be resolved before closure.

**EC-3: Multiple dispatches for same PR across different states.**
Priority: `active` > `completed` > `pending/staging` > `rejected`. If any dispatch is active, the PR is active. If none are active but one completed successfully, the PR is completed.

**EC-4: Dispatch exists for a PR ID not in FEATURE_PLAN.md.**
The dispatch is ignored for queue derivation purposes. It may be a leftover from a previous feature. A drift warning is raised.

**EC-5: FEATURE_PLAN.md lists a PR but no dispatch has ever been created.**
If dependencies are satisfied, state is `pending`. If dependencies are not satisfied, state is `blocked`. This is the normal initial state for PRs that have not yet been dispatched.

---

## 4. Projection Drift Detection

### 4.1 What Is Drift

Drift occurs when a queue projection file disagrees with the state derived from the source hierarchy. Examples:

| Projection Claims | Runtime Truth | Drift Type |
|-------------------|---------------|------------|
| PR-1 is "Queued" | Dispatch for PR-1 is in `active/` | **Under-reported active** |
| PR-0 is "In Progress" | Dispatch for PR-0 is in `completed/` | **Stale active** |
| "In Progress: None" | Dispatch exists in `active/` | **Missing active** |
| PR-2 is "Completed" | No dispatch for PR-2 in `completed/` | **Phantom completion** |
| PR-1 is "Blocked" | All dependencies are completed | **Stale blocked** |

### 4.2 Drift Detection Method

Drift is detected by comparing the derived state (Section 3) against the projection state. The comparison is a full diff — every PR in the queue is compared, not just the ones that changed recently.

```
For each PR in FEATURE_PLAN.md:
  derived_state  = derive_state(PR)           # Section 3.2 algorithm
  projected_state = read_projection(PR)        # From pr_queue_state.json or PR_QUEUE.md

  IF derived_state != projected_state:
    emit drift_warning(pr_id, derived_state, projected_state, evidence)
```

### 4.3 Drift Severity

| Severity | Condition | Effect |
|----------|-----------|--------|
| **blocking** | Derived state is `active` but projection says `pending` or `blocked` | Promotion or re-dispatch could create a duplicate active dispatch |
| **blocking** | Derived state is `completed` but projection says `active` | Dependent PRs may be unnecessarily blocked |
| **warning** | Derived state is `pending` but projection says `blocked` | PR is eligible but T0 may not see it |
| **warning** | Projection has a PR not in FEATURE_PLAN.md | Stale data from a previous feature |
| **info** | Derived and projected states agree but receipt confirmation is missing | Completion is structurally correct but evidence chain is incomplete |

### 4.4 When Drift Must Be Checked

Drift detection MUST run before:
1. **Dispatch promotion** — before moving a dispatch from staging to pending/queue.
2. **PR closure verification** — before declaring a PR complete.
3. **Feature closure verification** — before declaring a feature complete.
4. **T0 queue status read** — whenever T0 explicitly requests reconciled queue state.

Drift detection MAY run:
- On any `vnx status` invocation (advisory mode).
- After receipt processing completes (opportunistic refresh).

### 4.5 Drift Response

When blocking drift is detected:
- The operation that triggered drift detection MUST halt.
- The drift details MUST be surfaced to T0 or the operator.
- The projection MUST be regenerated from derived state before retrying.

When warning drift is detected:
- The operation MAY proceed.
- The drift details MUST be logged.
- The projection SHOULD be regenerated.

---

## 5. Reconciliation Rules

### 5.1 Reconciliation Is Deterministic

Given the same FEATURE_PLAN.md, the same dispatch directory contents, and the same receipt trail, reconciliation MUST produce the same queue state every time. There is no randomness, no timestamp-dependent ordering, and no dependency on prior reconciliation results.

### 5.2 Reconciliation Is Idempotent

Running reconciliation twice with no intervening state changes produces identical output. Reconciliation does not create side effects beyond updating projection files.

### 5.3 Reconciliation Overwrites Projections

When reconciliation runs, it replaces projection files entirely. It does not merge or patch. The projection is a pure function of the source hierarchy.

Affected files:
- `pr_queue_state.json` — rebuilt from derived state
- `pr_queue.json` — rebuilt from derived state
- `PR_QUEUE.md` — regenerated from derived state

### 5.4 Reconciliation Preserves Provenance

Each reconciled state entry includes provenance: the source that determined the state and the evidence path. This allows T0 to distinguish "completed because dispatch file exists in completed/" from "completed because projection said so."

```json
{
  "pr_id": "PR-0",
  "state": "completed",
  "provenance": {
    "source": "dispatch_filesystem",
    "evidence": ".vnx-data/dispatches/completed/20260331-212208-queue-truth-contract-and-sourc-C.md",
    "receipt_confirmed": true,
    "receipt_path": ".vnx-data/receipts/raw/20260331-...-receipt.json"
  }
}
```

### 5.5 Reconciliation Does Not Modify Source Data

Reconciliation reads from the source hierarchy and writes to projection files. It never modifies:
- FEATURE_PLAN.md
- Dispatch files (does not move, delete, or edit them)
- Receipt files
- Review gate results

---

## 6. Interaction With Existing Components

### 6.1 pr_queue_manager.py

The queue manager currently maintains `pr_queue_state.json` as a stateful store that is updated incrementally. Under this contract, reconciliation provides a parallel path that can rebuild the same state from scratch. The manager's incremental updates remain valid for normal operation; reconciliation is the corrective path when drift is detected.

PR-1 will implement the reconciliation path. This contract does not require rewriting pr_queue_manager.py — it requires adding a reconciliation function that can be called alongside or instead of incremental updates.

### 6.2 closure_verifier.py

The closure verifier checks gate evidence and review contract integrity. Under this contract, closure verification must also confirm that the queue state for the PR being closed matches derived state, not just projected state. PR-2 will integrate reconciliation into the closure path.

### 6.3 receipt_processor_v4.sh

The receipt processor generates receipts from unified reports. It does not need modification. Receipts are a source in the hierarchy (Priority 3), not a consumer. The reconciliation path reads receipts; it does not write them.

### 6.4 runtime_coordination.py

Runtime coordination manages dispatch leases and state transitions via SQLite. This contract does not replace or duplicate that layer. Runtime coordination answers "is this dispatch still alive?" while queue truth answers "what state is this PR in?" They are complementary.

### 6.5 t0_brief.json and generate_t0_brief.sh

The T0 brief is a consumer of queue state. After reconciliation, the brief should reflect reconciled truth. PR-2 will ensure the brief generation path uses reconciled state when available.

---

## 7. Non-Goals

This contract explicitly scopes out the following. Any PR work that drifts into these areas must be rejected or deferred.

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Full queue-engine rewrite | Reconciliation is additive. The existing queue manager continues to work. |
| NG-2 | Replacing the receipt processor | Receipts are a source, not a target for modification. |
| NG-3 | Replacing dispatch filesystem with a database | The filesystem layout is the runtime truth source. Changing it is a separate architectural decision. |
| NG-4 | Automated drift auto-repair without operator visibility | Drift is surfaced and projections are regenerated. Modifying source data (moving dispatch files, editing receipts) is not in scope. |
| NG-5 | Real-time push-based queue updates | Reconciliation is pull-based: run on demand or at decision points. Event-driven updates are a future concern. |
| NG-6 | Multi-feature queue merging | This contract covers one active feature at a time. Cross-feature queue coordination is a separate contract. |
| NG-7 | Modifying VNX core infrastructure (.vnx/) | This feature exercises and validates infrastructure, it does not change it. |
| NG-8 | Changing the dispatch state machine | Runtime coordination state transitions are unchanged. Queue truth reads dispatch state; it does not redefine it. |
| NG-9 | Performance optimization of reconciliation | Correctness first. The dispatch directory scan is bounded by the number of PRs in a feature (typically < 20). |

### 7.1 Scope Creep Detection

A PR is out of scope for this feature if it:
- Modifies files under `.vnx/`.
- Changes dispatch state machine transitions in `runtime_coordination.py`.
- Adds new receipt event types.
- Replaces incremental queue updates with reconciliation-only (both must coexist).
- Introduces cross-feature dependency resolution.

---

## 8. Contract Verification

### 8.1 How To Verify This Contract Is Satisfied

| # | Check | Method |
|---|-------|--------|
| V-1 | Source hierarchy is explicit | Sections 2.1 and 2.2 define all five sources and their priority |
| V-2 | State derivation is deterministic | Section 3.2 provides a step-by-step algorithm with no ambiguity |
| V-3 | All four states have derivation rules | Section 3.1 defines completed, active, pending, blocked |
| V-4 | Drift detection is defined | Section 4 defines what drift is, how to detect it, and severity classification |
| V-5 | Stale projection reliance is blocked | Section 4.4 mandates drift detection before promotion, closure, and feature completion |
| V-6 | Reconciliation is deterministic and idempotent | Sections 5.1 and 5.2 state this explicitly |
| V-7 | Non-goals prevent scope creep | Section 7 lists nine explicit non-goals with rationale |

### 8.2 Quality Gate Checklist

`gate_pr0_queue_truth_contract`:
- [ ] Contract defines source-of-truth priority among feature plan, dispatch files, receipts, and queue projections (Section 2)
- [ ] Contract defines deterministic rules for completed, active, pending, and blocked queue state (Section 3)
- [ ] Contract explains how projection drift is detected and surfaced (Section 4)
- [ ] Contract blocks silent reliance on stale queue projections during active execution (Section 4.4, 4.5)
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## Appendix A: Source-Evidence Matrix

Quick reference showing which source answers which question.

| Question | Primary Source | Confirming Source |
|----------|---------------|-------------------|
| What PRs exist in this feature? | FEATURE_PLAN.md | — |
| What are the dependencies between PRs? | FEATURE_PLAN.md | — |
| Is a PR currently being worked on? | Dispatch filesystem (`active/`) | Receipts (working events) |
| Has a PR been completed? | Dispatch filesystem (`completed/`) | Receipts (terminal events) |
| Is a PR eligible for dispatch? | Derived: dependencies completed + no active dispatch | — |
| What gate does a PR require? | FEATURE_PLAN.md (review-stack) | — |
| Has a gate passed? | Review gate results (`$VNX_STATE_DIR/review_gates/results/`) | Receipts |
| What does T0 see for queue status? | Queue projections (pr_queue_state.json, PR_QUEUE.md) | Must match derived state |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| 50_DOUBLE_FEATURE_TRIAL_CONTRACT.md | Trial exposed the queue drift problem this contract addresses |
| 45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md | Gate evidence is a source in the hierarchy (Priority 3) |
| 30_FPC_EXECUTION_CONTRACTS.md | Dispatch execution uses task class definitions from this contract |
| 42_FPD_PROVENANCE_CONTRACT.md | Provenance model informs the reconciliation provenance output |
| HEADLESS_RUN_CONTRACT.md | Headless runs produce receipts consumed by the hierarchy |
| STATE_MANAGEMENT.md | Progress state system is Priority 5 (advisory) in the hierarchy |

## Appendix C: Glossary

| Term | Definition |
|------|-----------|
| **Source hierarchy** | The ordered list of data sources used to derive queue state (Section 2) |
| **Derived state** | Queue state computed from the source hierarchy algorithm (Section 3) |
| **Projected state** | Queue state read from cached projection files |
| **Drift** | Disagreement between derived state and projected state |
| **Reconciliation** | The process of recomputing derived state and overwriting projections |
| **Provenance** | Metadata recording which source and evidence determined a state |
| **Unconfirmed completion** | A dispatch in `completed/` without a matching terminal receipt |
