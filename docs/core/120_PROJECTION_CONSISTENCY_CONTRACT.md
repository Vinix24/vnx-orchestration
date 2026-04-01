# Projection Consistency Contract

**Status**: Canonical
**Feature**: Queue And Runtime Projection Consistency Hardening
**PR**: PR-0
**Gate**: `gate_pr0_projection_consistency_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the consistency rules between canonical state surfaces and their projections. It specifies which contradictions are forbidden, which surface wins under mismatch, and what operator-visible diagnostics must exist when reconciliation is incomplete. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

VNX maintains multiple state surfaces that represent overlapping aspects of dispatch, terminal, and queue state. These surfaces were designed with clear authority hierarchy (Queue Truth Contract, doc 70; Terminal Exclusivity Contract, doc 80), but no contract defines what happens when these surfaces **contradict each other during live execution**.

Observed incidents from the first autonomous chain:

- **`In Progress: None` while work was visibly running**: `progress_state.yaml` showed track C as idle while `dispatches/active/` contained an active dispatch and the terminal was visibly executing. T0 nearly re-dispatched.
- **Queue shows `queued` for an active PR**: `pr_queue_state.json` reported PR-1 as `queued` while a dispatch for it was in `active/`. The projection update lagged behind the filesystem move.
- **Terminal `idle` in projection while lease is `leased`**: `terminal_state.json` showed T2 as `idle` after a shadow-write failure, while `terminal_leases` correctly showed `leased`. The dispatcher's dual-check (FC-6) caught this, but operator diagnostics showed contradictory status.

These are not display bugs. They are **state integrity failures** that erode operator trust and create decision hazards.

### 1.2 The Design Principle

Every operator-visible state surface must either:
1. **Derive from a single canonical source** and be provably consistent with it, OR
2. **Explicitly declare that reconciliation is incomplete** and show the divergence.

No projection may silently disagree with its canonical source. Silence is the enemy — contradictions must be visible, classified, and recoverable.

### 1.3 Relationship To Existing Contracts

This contract extends and does not replace:
- **Queue Truth Contract (70)**: Defines the 5-tier source hierarchy for queue state. This contract adds cross-surface consistency rules that doc 70 does not cover (terminal vs queue, runtime DB vs filesystem).
- **Terminal Exclusivity Contract (80)**: Defines canonical lease authority. This contract adds the projection-consistency obligations for `terminal_state.json` and `progress_state.yaml` relative to that canonical source.
- **Delivery Failure Lease Contract (90)**: Defines lease cleanup obligations. This contract adds the projection-update obligations that must accompany lease transitions.

---

## 2. State Surface Inventory

### 2.1 Canonical Surfaces

A **canonical surface** is a source of truth. It is written by a single authority, read by many consumers, and wins every dispute with a projection.

| ID | Surface | Storage | Authoritative For | Writer | Contract Reference |
|----|---------|---------|-------------------|--------|-------------------|
| **C-1** | `terminal_leases` (SQLite) | `runtime_coordination.db` | Terminal availability, lease ownership, dispatch assignment | `lease_manager.py`, `runtime_core.py` | Terminal Exclusivity (80) §2 |
| **C-2** | `dispatches` table (SQLite) | `runtime_coordination.db` | Dispatch lifecycle state (queued→claimed→delivering→completed) | `dispatch_broker.py`, `runtime_core.py` | Schema: `runtime_coordination.sql` |
| **C-3** | Dispatch filesystem directories | `.vnx-data/dispatches/{active,completed,pending,staging,rejected}/` | Runtime execution state — which PRs are active right now | Dispatcher (file moves) | Queue Truth (70) Rule P-2 |
| **C-4** | Receipt trail | `.vnx-data/receipts/`, `t0_receipts.ndjson` | Completion confirmation, gate evidence | `receipt_processor_v4.sh` | Queue Truth (70) Rule P-3 |
| **C-5** | `FEATURE_PLAN.md` | Project root | PR existence, dependency graph, structural metadata | Human / planner | Queue Truth (70) Rule P-1 |
| **C-6** | `coordination_events` (SQLite) | `runtime_coordination.db` | Immutable audit trail of all state transitions | Runtime coordination layer | Schema: `runtime_coordination.sql` |

### 2.2 Projected Surfaces

A **projected surface** is a derived view. It is computed from one or more canonical surfaces. It must never be the sole basis for dispatch, promotion, or closure decisions.

| ID | Surface | Storage | Derives From | Writer | Staleness Bound |
|----|---------|---------|-------------|--------|-----------------|
| **P-1** | `terminal_state.json` | `.vnx-data/state/` | C-1 (`terminal_leases`) | `terminal_state_shadow.py`, `lease_manager.py` | Must update within 2s of lease transition |
| **P-2** | `pr_queue_state.json` | `.vnx-data/state/` | C-3 (dispatch filesystem), C-4 (receipts), C-5 (FEATURE_PLAN.md) | `pr_queue_manager.py`, `queue_reconciler.py` | Must update within 5s of dispatch directory change |
| **P-3** | `progress_state.yaml` | `.vnx-data/state/` | C-4 (receipts), C-3 (dispatch filesystem) | `update_progress_state.py`, `receipt_processor` | Must update within 5s of receipt arrival |
| **P-4** | `PR_QUEUE.md` | Project root | P-2 (`pr_queue_state.json`) | Queue regeneration scripts | Same as P-2 |
| **P-5** | `dashboard_status.json` | `.vnx-data/state/` | P-1, P-2, P-3 (all projections) | Dashboard generators | Advisory — no staleness contract |
| **P-6** | `t0_brief.json` | `.vnx-data/state/` | P-2, P-3, C-3, C-4 | `generate_t0_brief.sh` | Advisory — refreshed on demand |

### 2.3 Derivation Graph

```
FEATURE_PLAN.md (C-5)
        |
        v
  [Queue Reconciler] <-- dispatch filesystem (C-3) + receipts (C-4)
        |
        v
  pr_queue_state.json (P-2) --> PR_QUEUE.md (P-4)
                                    |
                                    v
terminal_leases (C-1)          dashboard_status.json (P-5)
        |                           ^
        v                           |
  terminal_state.json (P-1) --------+
                                    |
dispatches (C-2) + receipts (C-4)   |
        |                           |
        v                           |
  progress_state.yaml (P-3) --------+
```

---

## 3. Forbidden Contradictions

A **forbidden contradiction** is a state where a projected surface disagrees with its canonical source in a way that could cause incorrect dispatch, promotion, or closure decisions. Forbidden contradictions are **defects**, not cosmetic drift.

### 3.1 Terminal State Contradictions

| # | Canonical State (C-1) | Projected State (P-1) | Classification | Risk |
|---|----------------------|----------------------|----------------|------|
| **FC-T1** | Lease state = `leased`, dispatch_id = D | terminal_state.json shows `idle` | **Forbidden** | Dispatcher may double-dispatch to a terminal that is already busy |
| **FC-T2** | Lease state = `idle`, dispatch_id = NULL | terminal_state.json shows `working`, claimed_by = D | **Forbidden** | Terminal is blocked from dispatch despite being available |
| **FC-T3** | Lease state = `expired` | terminal_state.json shows `working` | **Warning** | Operator sees "working" but lease has expired; reconciler should intervene |
| **FC-T4** | Lease state = `leased`, dispatch_id = D1 | terminal_state.json shows `working`, claimed_by = D2 | **Forbidden** | Projection points to wrong dispatch; receipt linkage breaks |
| **FC-T5** | Terminal does not exist in `terminal_leases` | terminal_state.json has an entry for it | **Warning** | Stale projection from removed terminal |

### 3.2 Queue State Contradictions

| # | Canonical State | Projected State (P-2) | Classification | Risk |
|---|----------------|----------------------|----------------|------|
| **FC-Q1** | Dispatch for PR in `active/` (C-3) | pr_queue_state.json shows PR as `queued` or `blocked` | **Forbidden** | T0 may create duplicate dispatch for already-active PR |
| **FC-Q2** | Dispatch for PR in `completed/` (C-3) | pr_queue_state.json shows PR as `active` | **Forbidden** | Dependent PRs remain blocked despite dependency being satisfied |
| **FC-Q3** | No dispatch exists for PR | pr_queue_state.json shows PR as `active` or `completed` | **Forbidden** | Phantom progress — PR appears done but no evidence exists |
| **FC-Q4** | PR not in FEATURE_PLAN.md (C-5) | pr_queue_state.json contains the PR | **Warning** | Stale entry from previous feature |
| **FC-Q5** | All dependencies completed | pr_queue_state.json shows PR as `blocked` | **Warning** | PR is eligible but operator cannot see it |

### 3.3 Progress State Contradictions

| # | Canonical State | Projected State (P-3) | Classification | Risk |
|---|----------------|----------------------|----------------|------|
| **FC-P1** | Dispatch for track in `active/` (C-3) | progress_state.yaml shows track as `idle` | **Forbidden** | T0 sees "no work in progress" while work is running — the observed incident |
| **FC-P2** | No dispatch for track in `active/` (C-3) | progress_state.yaml shows track as `working` | **Warning** | Stale working state after dispatch completed or failed |
| **FC-P3** | Receipt confirms gate passed (C-4) | progress_state.yaml shows earlier gate | **Warning** | Gate progression is behind; T0 may not see available next steps |
| **FC-P4** | progress_state.yaml shows `active_dispatch_id = D` | No dispatch D in `active/` (C-3) | **Forbidden** | Progress points to non-existent or completed dispatch |

### 3.4 Cross-Surface Contradictions

| # | Surface A | Surface B | Classification | Risk |
|---|-----------|-----------|----------------|------|
| **FC-X1** | C-1: lease `leased` for terminal T, dispatch D | C-3: dispatch D not in `active/` | **Forbidden** | Lease held but dispatch is not active — stranded terminal |
| **FC-X2** | C-2: dispatch D state = `completed` | C-3: dispatch D file still in `active/` | **Warning** | DB says done, filesystem says active — stale file move |
| **FC-X3** | C-3: dispatch D in `completed/` | C-4: no receipt for dispatch D | **Warning** | Unconfirmed completion (EC-2 in Queue Truth Contract) |
| **FC-X4** | P-1: terminal T `working`, dispatch D | P-3: track for D shows `idle` | **Forbidden** | Terminal and progress projections contradict — one was not updated |

---

## 4. Tie-Break Rules

When surfaces contradict, a deterministic rule resolves the conflict. These rules extend the Queue Truth Contract (70) priority hierarchy with terminal and progress state.

### 4.1 Terminal State Tie-Break

| Rule | Conflict | Winner | Action |
|------|----------|--------|--------|
| **TB-T1** | C-1 vs P-1 (terminal_leases vs terminal_state.json) | **C-1 always wins** | Regenerate P-1 from C-1. Log `projection_overwritten`. |
| **TB-T2** | C-1 unreadable, P-1 exists | **P-1 is not trusted** | Terminal state = `ambiguous` (Terminal Exclusivity §2.1). Block dispatch. |
| **TB-T3** | Both C-1 and P-1 unreadable | **State = ambiguous** | Block dispatch, emit incident. |

### 4.2 Queue State Tie-Break

| Rule | Conflict | Winner | Action |
|------|----------|--------|--------|
| **TB-Q1** | C-3 vs P-2 (dispatch filesystem vs pr_queue_state.json) | **C-3 always wins** | Run queue reconciliation. Regenerate P-2 from C-3 + C-4 + C-5. |
| **TB-Q2** | C-3 vs C-2 (filesystem vs SQLite dispatch state) | **C-3 wins for execution state** | Filesystem directory determines "is this active right now." DB state records the lifecycle. If DB says `completed` but file is in `active/`, trust the filesystem and flag the DB for reconciliation. |
| **TB-Q3** | C-4 vs C-3 (receipt says complete, filesystem says active) | **C-3 wins (file is still in active/)** | The dispatch has not been moved yet. Receipt arrived early or file move failed. Do not treat as completed until file is in `completed/`. |

### 4.3 Progress State Tie-Break

| Rule | Conflict | Winner | Action |
|------|----------|--------|--------|
| **TB-P1** | C-3 vs P-3 (active dispatch exists but progress shows idle) | **C-3 always wins** | Regenerate P-3 from C-3 + C-4. This is the fix for the observed `In Progress: None` incident. |
| **TB-P2** | C-4 vs P-3 (receipt shows gate passed but progress is behind) | **C-4 wins** | Advance P-3 gate to match receipt evidence. |

### 4.4 Cross-Surface Tie-Break

| Rule | Conflict | Winner | Action |
|------|----------|--------|--------|
| **TB-X1** | C-1 lease held but C-3 dispatch not in active/ | **C-1 wins (lease is real)** | Do not release lease. Flag as stranded. Reconciler investigates. |
| **TB-X2** | P-1 and P-3 contradict (terminal working, progress idle) | **Neither wins — both regenerate** | Regenerate P-1 from C-1 and P-3 from C-3 + C-4. Contradiction is resolved by recomputing both from canonical sources. |

---

## 5. Consistency Invariants

These invariants must hold at all times. Violation of any invariant is a defect.

### 5.1 Invariant Table

| ID | Invariant | Canonical Surfaces | Check Method |
|----|-----------|-------------------|--------------|
| **CI-1** | If a dispatch is in `active/` (C-3), the terminal_leases row (C-1) for its terminal must have `state = leased` and `dispatch_id` matching | C-1 + C-3 | Compare lease dispatch_id with active dispatch metadata |
| **CI-2** | If a dispatch is in `active/` (C-3), progress_state.yaml (P-3) must show the corresponding track as `working` with matching `active_dispatch_id` | C-3 + P-3 | Compare active dispatch with progress track state |
| **CI-3** | If terminal_leases (C-1) shows `leased`, terminal_state.json (P-1) must show `working` with matching `claimed_by` | C-1 + P-1 | Compare lease state with projection status |
| **CI-4** | If pr_queue_state.json (P-2) shows a PR as `active`, a dispatch for that PR must exist in `active/` (C-3) | P-2 + C-3 | Reverse-verify projection against filesystem |
| **CI-5** | If pr_queue_state.json (P-2) shows a PR as `completed`, a dispatch for that PR must exist in `completed/` (C-3) | P-2 + C-3 | Reverse-verify projection against filesystem |
| **CI-6** | If progress_state.yaml (P-3) shows a track as `working`, terminal_state.json (P-1) for that track's terminal must also show `working` | P-3 + P-1 | Cross-projection consistency |

### 5.2 Invariant Checking Frequency

| Trigger | Invariants Checked | Mandatory |
|---------|-------------------|-----------|
| Before dispatch delivery | CI-1 (lease matches active) | Yes |
| After dispatch delivery | CI-2, CI-3 (progress and terminal updated) | Yes |
| After receipt processing | CI-2, CI-4, CI-5 (queue and progress reflect receipt) | Yes |
| Before PR closure | CI-4, CI-5 (queue reflects completion) | Yes |
| On `vnx status` | All (CI-1 through CI-6) | Advisory |
| Reconciler periodic run | All (CI-1 through CI-6) | Yes |

---

## 6. Staleness Bounds

### 6.1 Projection Update Deadlines

Each projection must update within a bounded time after its canonical source changes. Staleness beyond these bounds is a defect.

| Projection | Canonical Trigger | Max Staleness | Enforcement |
|------------|------------------|---------------|-------------|
| P-1 (`terminal_state.json`) | Lease acquired, released, or expired in C-1 | **2 seconds** | Shadow writer must fire after every lease operation |
| P-2 (`pr_queue_state.json`) | Dispatch file moved between directories in C-3 | **5 seconds** | Queue manager update or reconciler must fire after dispatch state change |
| P-3 (`progress_state.yaml`) | Receipt arrives in C-4, or dispatch becomes active in C-3 | **5 seconds** | Receipt processor or dispatcher must update progress after state change |
| P-4 (`PR_QUEUE.md`) | P-2 updates | **Same as P-2** | Regenerated alongside P-2 |
| P-5 (`dashboard_status.json`) | Any projection updates | **No contract** | Advisory only, refreshed on demand |

### 6.2 Staleness Detection

A projection is stale when:
- Its `updated_at` timestamp is older than the staleness bound after the last canonical state change, OR
- Its content contradicts the current canonical state (a forbidden contradiction from Section 3).

Staleness detection does not require wall-clock comparison. It is sufficient to check that the projection content matches the canonical state at the time of the check.

---

## 7. Operator-Visible Diagnostics

### 7.1 Mismatch Report

When consistency checking detects a contradiction, the system must produce a **mismatch report** visible to the operator or T0. The report includes:

| Field | Content |
|-------|---------|
| `contradiction_id` | FC-* code from Section 3 |
| `severity` | `forbidden` or `warning` |
| `canonical_surface` | Which canonical source has the authoritative value |
| `canonical_value` | The value from the canonical source |
| `projected_surface` | Which projection has the contradictory value |
| `projected_value` | The value from the projection |
| `tie_break_rule` | TB-* code from Section 4 |
| `recommended_action` | What the operator or reconciler should do |
| `auto_resolved` | Whether the system can fix this automatically (projection regeneration) |
| `timestamp` | When the mismatch was detected |

### 7.2 Mismatch Severity And Response

| Severity | Meaning | Operator Impact | System Response |
|----------|---------|----------------|-----------------|
| **forbidden** | Canonical and projected surfaces contradict in a way that causes incorrect decisions | **Must be resolved before next dispatch/promotion/closure** | Block the triggering operation. Emit mismatch report. Attempt automatic reconciliation. If auto-reconciliation succeeds, log and proceed. If it fails, escalate to operator. |
| **warning** | Surfaces disagree but the contradiction does not cause immediate decision errors | **Should be resolved, not blocking** | Log mismatch report. Attempt automatic reconciliation in background. Operation may proceed. |
| **info** | Mild staleness or missing confirmation (e.g., unconfirmed completion) | **Informational only** | Log. No action required. |

### 7.3 Diagnostic Commands

The following diagnostic capabilities must be available to T0 and operators:

| Capability | Purpose | Output |
|-----------|---------|--------|
| **Consistency check** | Compare all projections against canonical sources | List of all contradictions with FC-* codes and severity |
| **Reconcile** | Regenerate all projections from canonical sources | Updated projection files + list of corrections made |
| **Explain state** | For a given PR or terminal, show the canonical state, projected state, and provenance | Single-entity deep inspection |
| **History** | For a given entity, show the coordination events timeline | Ordered list of state transitions from C-6 |

### 7.4 Diagnostic Output Location

Mismatch reports are written to:
- **Structured**: `$VNX_STATE_DIR/consistency_checks/` as NDJSON (one event per line)
- **Operator-readable**: Included in `vnx status` output when contradictions exist
- **T0 brief**: Forbidden contradictions must appear in `t0_brief.json` as blocking alerts

---

## 8. Reconciliation Protocol

### 8.1 What Reconciliation Does

Reconciliation reads all canonical surfaces (C-1 through C-5) and regenerates all projections (P-1 through P-4). It is:
- **Deterministic**: Same canonical state produces same projections.
- **Idempotent**: Running twice with no intervening changes produces identical results.
- **Non-destructive**: Reconciliation never modifies canonical surfaces. It only overwrites projections.
- **Auditable**: Every correction is logged with before/after values and the canonical evidence.

### 8.2 Reconciliation Sequence

```
1. Read C-5 (FEATURE_PLAN.md) → extract valid PR IDs and dependency graph
2. Scan C-3 (dispatch filesystem) → determine active/completed/pending state per PR
3. Read C-4 (receipts) → confirm completions, extract gate evidence
4. Read C-1 (terminal_leases) → determine terminal states
5. Read C-2 (dispatches table) → cross-reference with filesystem state

6. Regenerate P-2 (pr_queue_state.json) from steps 1-3
7. Regenerate P-4 (PR_QUEUE.md) from step 6
8. Regenerate P-1 (terminal_state.json) from step 4
9. Regenerate P-3 (progress_state.yaml) from steps 2-4

10. Run consistency check (Section 5.1) across all surfaces
11. Emit mismatch report for any remaining contradictions
12. Log reconciliation_completed event to C-6
```

### 8.3 When Reconciliation Must Run

| Trigger | Mandatory | Scope |
|---------|-----------|-------|
| Before dispatch delivery | Yes | P-1 (terminal projection for target terminal) |
| After successful delivery | Yes | P-2, P-3 (queue and progress for dispatched PR/track) |
| After receipt processing | Yes | P-2, P-3 (queue and progress for completed PR/track) |
| Before PR closure | Yes | Full (all projections) |
| Before feature closure | Yes | Full (all projections) |
| On `vnx reconcile` command | Yes | Full (all projections) |
| Periodic (reconciler timer) | Recommended | Full (all projections), every 60 seconds during active execution |

### 8.4 Reconciliation Failure

If reconciliation cannot read a canonical source (DB connection fails, filesystem error):
- The affected projections are **not updated** (stale is better than wrong).
- The reconciliation logs the failure with the specific canonical source that was unreadable.
- The affected projections are marked as `reconciliation_incomplete` in their metadata.
- Any consistency check against those projections must treat them as `ambiguous` (not trusted).

---

## 9. Implementation Constraints For PR-1

PR-1 (Reconciliation Engine) implements against this contract. The following constraints are binding:

1. **Invariants CI-1 through CI-6** (Section 5.1) must be checkable by the reconciliation engine.
2. **Forbidden contradictions** (Section 3, all FC-* entries classified as `forbidden`) must be detected and reported.
3. **Tie-break rules** (Section 4) must be applied when contradictions are found: canonical wins, projection is regenerated.
4. **Staleness bounds** (Section 6.1) must be enforced: projection updates fire within the specified deadlines after canonical state changes.
5. **Mismatch reports** (Section 7.1) must include all required fields.
6. **Reconciliation** (Section 8) must be deterministic, idempotent, and non-destructive.
7. **The `In Progress: None` incident** (FC-P1) must be directly addressed: progress_state.yaml must reflect active dispatches within 5 seconds.

---

## 10. Verification Criteria For PR-2

PR-2 (Certification) certifies this contract by reproducing observed drift incidents. The following must be demonstrated:

1. The `In Progress: None` while work is running scenario (FC-P1) is reproduced and fixed.
2. Queue projection lag behind dispatch filesystem (FC-Q1) is reproduced and fixed.
3. Terminal projection divergence from canonical lease (FC-T1, FC-T4) is reproduced and detected.
4. Consistency checks detect all forbidden contradictions from Section 3.
5. Reconciliation resolves contradictions by regenerating projections from canonical sources.
6. Operator diagnostics correctly surface unresolved mismatches.

---

## Appendix A: Contradiction Quick Reference

All contradictions from Section 3, sorted by severity:

### Forbidden (Must Block Operations)

| Code | Description |
|------|-------------|
| FC-T1 | Lease `leased` but projection shows `idle` |
| FC-T2 | Lease `idle` but projection shows `working` |
| FC-T4 | Lease dispatch_id mismatch with projection |
| FC-Q1 | Active dispatch but queue shows `queued`/`blocked` |
| FC-Q2 | Completed dispatch but queue shows `active` |
| FC-Q3 | No dispatch but queue shows `active`/`completed` |
| FC-P1 | Active dispatch but progress shows track `idle` |
| FC-P4 | Progress points to non-existent active dispatch |
| FC-X1 | Lease held but dispatch not in `active/` |
| FC-X4 | Terminal `working` but progress `idle` (cross-projection) |

### Warning (Log And Auto-Repair)

| Code | Description |
|------|-------------|
| FC-T3 | Lease `expired` but projection shows `working` |
| FC-T5 | Projection for non-existent terminal |
| FC-Q4 | Queue contains PR not in FEATURE_PLAN.md |
| FC-Q5 | All dependencies met but queue shows `blocked` |
| FC-P2 | No active dispatch but progress shows `working` |
| FC-P3 | Receipt shows gate passed but progress is behind |
| FC-X2 | DB says `completed` but file in `active/` |
| FC-X3 | File in `completed/` but no receipt |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Queue Truth (70) | This contract extends doc 70's source hierarchy with cross-surface consistency rules. Doc 70 defines priority; this contract defines forbidden contradictions and tie-breaks. |
| Terminal Exclusivity (80) | This contract adds projection-consistency obligations for `terminal_state.json` relative to the canonical `terminal_leases` table defined in doc 80. |
| Delivery Failure Lease (90) | This contract adds projection-update obligations that accompany lease cleanup after delivery failure. |
| Input-Ready Terminal (110) | Orthogonal — doc 110 governs tmux pane input mode, not state surface consistency. |
| State Management (technical) | This contract formalizes the relationship between `progress_state.yaml` and its canonical sources that the state management doc describes informally. |

## Appendix C: Glossary

| Term | Definition |
|------|-----------|
| **Canonical surface** | A source of truth that wins every dispute with a projection |
| **Projected surface** | A derived view computed from canonical sources, never primary truth |
| **Forbidden contradiction** | A disagreement between canonical and projected state that can cause incorrect decisions |
| **Tie-break rule** | A deterministic rule that resolves contradictions: canonical wins |
| **Consistency invariant** | A condition that must hold across surfaces at all times |
| **Staleness bound** | Maximum time a projection may lag behind its canonical source |
| **Mismatch report** | Structured diagnostic output when a contradiction is detected |
| **Reconciliation** | The process of regenerating projections from canonical sources |
