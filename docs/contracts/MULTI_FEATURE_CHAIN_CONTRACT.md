# Multi-Feature Chain Execution Contract

**Status**: Accepted
**PR**: PR-0
**Gate**: gate_pr0_chain_contract_and_recovery_policy
**Date**: 2026-04-02
**Author**: T3 (Track C Architecture)

This document defines the canonical lifecycle for multi-feature chain execution in VNX: how features advance through a chain, when interrupted chains can resume or must requeue, how branch/worktree transitions are governed, and how findings carry forward across feature boundaries.

All subsequent implementation PRs (PR-1 through PR-4) share this contract as their single source of truth for chain behavior.

---

## 1. Chain Identity and Scope

### 1.1 Definition

A **multi-feature chain** is an ordered sequence of features from a single `FEATURE_PLAN.md` that T0 executes in dependency order. Each feature in the chain produces one or more PRs, each PR progresses through dispatch, execution, review gates, and merge. The chain advances when a feature completes all its PRs and merges to `main`.

A chain is **not** a single long-running process. It is a governed sequence of discrete feature executions connected by advancement rules, carry-forward state, and branch discipline.

### 1.2 Chain Identity Fields

Every chain execution MUST be identifiable by these fields, recorded when the chain is initialized:

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `chain_id` | `str` | Generated (feature-plan hash + timestamp) | Unique identifier for this chain execution |
| `feature_plan` | `str` | `FEATURE_PLAN.md` path | The feature plan governing this chain |
| `feature_sequence` | `list[str]` | Dependency flow in plan | Ordered list of feature/PR identifiers |
| `chain_origin_sha` | `str` | `git rev-parse main` at chain start | The `main` commit the chain was initialized from |
| `initiated_by` | `str` | T0 dispatch | Actor who started the chain |
| `initiated_at` | `str` | ISO 8601 | Timestamp of chain initialization |

### 1.3 Chain Invariants

- **C-1**: A chain executes features in the order defined by the dependency flow in `FEATURE_PLAN.md`. No feature may begin until all its dependencies are merged to `main`.
- **C-2**: Each feature in the chain is executed through the normal dispatch lifecycle (staging -> queue -> active -> completed). The chain does not bypass dispatch governance.
- **C-3**: A chain has exactly one active feature at any time. Parallel feature execution within a single chain is not permitted.
- **C-4**: The chain maintains cumulative state (findings, open items, residual risks) that persists across feature boundaries.

---

## 2. Chain State Model

### 2.1 Chain States

A chain progresses through these states. Each transition is recorded in chain state.

```
INITIALIZED -> FEATURE_ACTIVE -> FEATURE_ADVANCING -> FEATURE_ACTIVE -> ... -> CHAIN_COMPLETE
                    |                   |
                    v                   v
               FEATURE_FAILED    ADVANCEMENT_BLOCKED
                    |                   |
                    v                   v
               RECOVERY_PENDING   CHAIN_HALTED
                    |
                    v
               FEATURE_ACTIVE (requeue) or CHAIN_HALTED (blocked/escalated)
```

### 2.2 State Definitions

| State | Description | Entry Condition |
|-------|-------------|-----------------|
| `INITIALIZED` | Chain created, feature sequence validated, no work started | T0 initializes chain from FEATURE_PLAN.md |
| `FEATURE_ACTIVE` | Current feature's PRs are being dispatched and executed | First feature starts or advancement succeeds |
| `FEATURE_FAILED` | Current feature's active dispatch failed or was rejected | Dispatch execution fails, review gate rejects, or CI fails |
| `RECOVERY_PENDING` | Failed feature is being evaluated for requeue, block, or escalation | Automatic on FEATURE_FAILED entry |
| `FEATURE_ADVANCING` | Current feature completed all PRs and merged; chain preparing to advance | All PRs for current feature merged to `main` with green CI |
| `ADVANCEMENT_BLOCKED` | Advancement conditions not met (stale baseline, unresolved blockers, gate failures) | Advancement pre-checks fail |
| `CHAIN_HALTED` | Chain cannot proceed without human intervention | Escalation from RECOVERY_PENDING or ADVANCEMENT_BLOCKED |
| `CHAIN_COMPLETE` | All features in chain completed successfully | Last feature in sequence advances |

### 2.3 State Transition Rules

| From | To | Condition |
|------|-----|-----------|
| `INITIALIZED` | `FEATURE_ACTIVE` | T0 dispatches first feature's first PR |
| `FEATURE_ACTIVE` | `FEATURE_ADVANCING` | All PRs for current feature: merged to `main`, CI green, all required gates passed |
| `FEATURE_ACTIVE` | `FEATURE_FAILED` | Any PR dispatch fails, review gate blocks, or CI fails after max retry |
| `FEATURE_FAILED` | `RECOVERY_PENDING` | Automatic — evaluate recovery policy (Section 4) |
| `RECOVERY_PENDING` | `FEATURE_ACTIVE` | Recovery policy determines: requeue (same feature, fresh attempt) |
| `RECOVERY_PENDING` | `CHAIN_HALTED` | Recovery policy determines: block or escalate |
| `FEATURE_ADVANCING` | `FEATURE_ACTIVE` | Advancement pre-checks pass (Section 5), next feature dispatched |
| `FEATURE_ADVANCING` | `ADVANCEMENT_BLOCKED` | Advancement pre-checks fail |
| `ADVANCEMENT_BLOCKED` | `FEATURE_ADVANCING` | Blocking condition resolved by operator |
| `ADVANCEMENT_BLOCKED` | `CHAIN_HALTED` | Operator determines chain cannot continue |
| `FEATURE_ACTIVE` | `CHAIN_COMPLETE` | Current feature is last in sequence AND all its PRs merged with green CI |

### 2.4 Stop Conditions

The chain MUST halt (enter `CHAIN_HALTED`) when any of these conditions are true:

1. **Unresolvable failure**: A feature fails and recovery policy classifies it as non-recoverable (Section 4.3)
2. **Escalation threshold**: A feature has been requeued more than **2 times** for the same failure class
3. **Baseline divergence**: `main` has diverged in a way that invalidates the chain's assumptions (e.g., conflicting commits from another chain)
4. **Blocking open items**: Carry-forward open items with severity `blocker` remain unresolved at feature boundary
5. **Gate provider unavailable**: A required review gate provider (Gemini, Codex) is unavailable and cannot be retried within the feature's execution window
6. **Operator halt**: T0 or human operator explicitly halts the chain

---

## 3. Feature Completion and Advancement Rules

### 3.1 Feature Completion Criteria

A feature within the chain is complete when ALL of the following are true:

1. Every PR defined in the feature plan has been dispatched, executed, and merged to `main`
2. Every required review gate (Gemini review, Codex final gate) has terminal success with:
   - Request record in `.vnx-data/state/review_gates/requests/`
   - Result record in `.vnx-data/state/review_gates/results/` with non-empty `contract_hash` and `report_path`
   - Normalized markdown report in `$VNX_DATA_DIR/unified_reports/`
3. GitHub Actions CI is green on the merge commit
4. All open items created during this feature are either:
   - Closed with evidence, OR
   - Explicitly deferred with severity < `blocker`, OR
   - Carried forward into the chain's cumulative open items (Section 6)
5. The feature's final PR receipt has been processed and recorded in `t0_receipts.ndjson`

### 3.2 Advancement Pre-checks

Before advancing to the next feature, T0 MUST verify:

| Check | Method | Failure Action |
|-------|--------|----------------|
| All current feature PRs merged | `pr_queue_state.json` shows all PRs completed | Block advancement |
| `main` is at expected SHA | `git rev-parse main` matches last merged PR's merge commit | Block advancement |
| No blocker open items | `open_items_manager.py digest` shows zero blocker-severity items | Block advancement |
| Required gates all passed | Review gate results directory has terminal success for every PR | Block advancement |
| Chain carry-forward ledger updated | Findings from current feature added to chain carry-forward | Block advancement |
| Worktree clean or removed | Current feature's worktree has no uncommitted changes | Block advancement |

### 3.3 Advancement Sequence

When all pre-checks pass, advancement proceeds as:

1. Record current feature as complete in chain state
2. Update chain carry-forward ledger with current feature's findings and deferred items
3. Remove or archive current feature's worktree
4. Create new branch from post-merge `main` for next feature (Section 5)
5. Initialize next feature's PR queue from `FEATURE_PLAN.md`
6. Dispatch first PR of next feature
7. Transition chain state to `FEATURE_ACTIVE` with next feature

---

## 4. Recovery and Requeue Policy

### 4.1 Failure Classification

When a feature attempt fails, the failure is classified into one of three categories:

| Class | Description | Examples |
|-------|-------------|----------|
| `recoverable_transient` | Temporary infrastructure or provider failure; retrying the same work is expected to succeed | Network timeout, API rate limit, provider temporary outage, CI flake |
| `recoverable_fixable` | The failure has a known fix path that can be applied within the current feature scope | Lint error, test failure from code bug, review gate finding with clear fix |
| `non_recoverable` | The failure cannot be resolved within current feature scope or indicates a fundamental design problem | Dependency conflict with another chain, architectural incompatibility, persistent provider failure |

### 4.2 Recovery Decision Tree

```
Feature attempt fails
  |
  +-- Is the failure transient? (provider outage, network, CI flake)
  |     YES -> REQUEUE (same dispatch, fresh attempt, max 2 retries)
  |     NO  -> Continue
  |
  +-- Is there a known fix within feature scope?
  |     YES -> REQUEUE (new dispatch with fix applied, max 2 retries per failure class)
  |     NO  -> Continue
  |
  +-- Is the failure blocking the entire chain?
  |     YES -> ESCALATE to CHAIN_HALTED (requires human intervention)
  |     NO  -> BLOCK current feature, attempt next if independent (rare; requires explicit dependency override)
```

### 4.3 Requeue Rules

- **R-1**: A requeued feature attempt creates a new dispatch with a new `dispatch_id`. The failed dispatch is marked `failed` with the failure class recorded.
- **R-2**: Maximum **2 requeue attempts** per failure class per feature. After 2 retries of the same class, escalate to `CHAIN_HALTED`.
- **R-3**: Total maximum **3 requeue attempts** per feature across all failure classes. After 3 total retries, escalate regardless.
- **R-4**: Each requeue attempt MUST start from the current `main` HEAD, not from the failed attempt's branch state.
- **R-5**: Requeue attempts inherit the chain's carry-forward ledger plus any new findings from the failed attempt.

### 4.4 Escalation Protocol

When a failure escalates to `CHAIN_HALTED`:

1. Chain state records the halting reason, failed feature, failure class, and attempt history
2. All carry-forward state is preserved (not discarded)
3. T0 emits an escalation receipt to `t0_receipts.ndjson` with `event_type: chain_halted`
4. The chain remains in `CHAIN_HALTED` until a human operator either:
   - Resolves the blocking condition and resumes the chain (returns to `RECOVERY_PENDING` for re-evaluation)
   - Terminates the chain (final state, all carry-forward preserved for post-mortem)

---

## 5. Branch and Worktree Transition Rules

### 5.1 Branch Naming Convention

Each feature in the chain uses a branch derived from the feature plan:

```
feature/<feature-slug>
```

Example: `feature/multi-feature-autonomy-hardening`

### 5.2 Worktree Creation Rules

- **W-1**: Every feature MUST execute in a worktree branched from the current `main` HEAD at the time of feature start.
- **W-2**: The worktree branch MUST be created AFTER the previous feature's last PR is merged and `main` is updated.
- **W-3**: Before creating the new worktree, verify: `git merge-base --is-ancestor <previous-feature-merge-sha> HEAD` on `main`. If false, `main` has diverged unexpectedly — block advancement.
- **W-4**: The worktree path follows: `.claude/worktrees/<feature-slug>` or a terminal-assigned path.

### 5.3 Worktree Transition Sequence

Between features, the worktree transition follows this exact sequence:

```
1. Verify current feature worktree has no uncommitted changes
2. Record current worktree state (branch, HEAD SHA, dirty status)
3. Remove or archive current feature's worktree
4. Fetch and verify main:
   a. git fetch origin main
   b. git checkout main && git pull
   c. Verify HEAD matches expected post-merge SHA
5. Create new worktree for next feature:
   a. git worktree add <path> -b feature/<next-feature-slug>
   b. Verify new worktree HEAD == main HEAD
6. Record new worktree provenance in chain state
```

### 5.4 Stale Branch Prevention

- **S-1**: No feature dispatch may execute on a branch whose merge-base with `main` is older than the most recent feature merge. This prevents stale-branch drift.
- **S-2**: Before any dispatch execution, verify: `git merge-base <feature-branch> main` returns a SHA that is equal to or newer than the chain's last recorded merge SHA.
- **S-3**: If stale branch is detected, the dispatch MUST be rejected and the worktree recreated from current `main`.

### 5.5 Worktree Cleanup

- On feature completion: worktree is archived (branch kept for audit) or removed (branch deleted after merge confirmation).
- On chain halt: worktree is preserved for post-mortem investigation. It is NOT automatically cleaned up.
- On chain completion: all feature worktrees may be cleaned up after final certification confirms all branches are merged.

---

## 6. Carry-Forward Rules

### 6.1 Carry-Forward Ledger

The chain maintains a cumulative carry-forward ledger that persists across feature boundaries. This ledger is stored in `.vnx-data/state/chain_carry_forward.json` and contains:

| Field | Type | Description |
|-------|------|-------------|
| `chain_id` | `str` | Chain this ledger belongs to |
| `findings` | `list[Finding]` | Accumulated findings from all features |
| `open_items` | `list[OpenItem]` | Open items not yet resolved |
| `deferred_items` | `list[DeferredItem]` | Items explicitly deferred with reason |
| `residual_risks` | `list[Risk]` | Identified risks that persist across features |
| `feature_summaries` | `list[FeatureSummary]` | Per-feature completion summary with evidence pointers |

### 6.2 Finding Carry-Forward Rules

- **F-1**: Every finding produced during a feature (from review gates, T3 analysis, test results) is recorded in the carry-forward ledger with its source feature, severity, and resolution status.
- **F-2**: Findings with severity `blocker` that are unresolved at feature boundary MUST halt the chain (stop condition 4 in Section 2.4).
- **F-3**: Findings with severity `warn` are carried forward and visible in the next feature's dispatch context. They do not block advancement but MUST be acknowledged.
- **F-4**: Findings with severity `info` are carried forward for audit completeness. They do not require acknowledgment.
- **F-5**: A finding is resolved when it is closed in `open_items.json` with evidence (code reference, test result, or review confirmation). Resolved findings remain in the ledger as closed records.

### 6.3 Open Item Carry-Forward

Open items created during feature execution follow the existing `open_items_manager.py` lifecycle with these chain-specific additions:

- **O-1**: At feature boundary, all open items are snapshotted into the carry-forward ledger with their current status.
- **O-2**: Open items with status `open` and severity `blocker` prevent chain advancement (Section 3.2).
- **O-3**: Open items may be deferred across a feature boundary ONLY if their severity is `warn` or `info` AND a deferral reason is recorded.
- **O-4**: The next feature's dispatch context MUST include a summary of carried-forward open items so workers are aware of accumulated debt.
- **O-5**: Open items are cumulative — they are never silently dropped between features.

### 6.4 Residual Risk Carry-Forward

- **RR-1**: Each feature's certification may identify residual risks (risks that are known but accepted for the current scope).
- **RR-2**: Residual risks are recorded in the carry-forward ledger with: risk description, accepting feature, acceptance rationale, and mitigation plan (if any).
- **RR-3**: At chain completion, the final certification MUST enumerate all residual risks from all features and confirm they are either mitigated or explicitly accepted.
- **RR-4**: A residual risk from an earlier feature that becomes a blocker in a later feature MUST escalate to `CHAIN_HALTED` with a reference to the originating feature's acceptance decision.

### 6.5 Feature Summary Records

At each feature boundary, a feature summary is appended to the carry-forward ledger:

```json
{
  "feature_id": "PR-0",
  "feature_name": "Multi-Feature Chain Contract",
  "status": "completed",
  "completed_at": "2026-04-02T...",
  "prs_merged": ["PR-0"],
  "merge_shas": ["abc1234"],
  "gate_results": {
    "gemini_review": "passed",
    "codex_gate": "passed"
  },
  "findings_created": 2,
  "findings_resolved": 2,
  "open_items_created": 1,
  "open_items_resolved": 0,
  "open_items_deferred": 1,
  "residual_risks": 0,
  "requeue_count": 0
}
```

---

## 7. Chain Observability

### 7.1 Chain State Projection

The chain state MUST be queryable from a single surface that shows:

- Current chain state (Section 2.2)
- Active feature and its progress (which PRs complete, which pending)
- Carry-forward summary (count of open findings, open items, residual risks)
- Requeue history (how many retries, for which features)
- Next feature in sequence (or CHAIN_COMPLETE/CHAIN_HALTED indicator)

### 7.2 Audit Trail

Every chain state transition MUST be recorded in `.vnx-data/state/chain_audit.jsonl` with:

```json
{
  "chain_id": "...",
  "timestamp": "ISO 8601",
  "from_state": "FEATURE_ACTIVE",
  "to_state": "FEATURE_ADVANCING",
  "feature_id": "PR-0",
  "actor": "T0",
  "reason": "All PRs merged, gates passed, no blockers",
  "evidence": { "merge_sha": "abc1234", "gate_results": "..." }
}
```

### 7.3 Receipt Integration

Chain events integrate with the existing receipt pipeline:

- `chain_initialized`: Chain created with feature sequence
- `feature_started`: Feature dispatch begins within chain
- `feature_completed`: Feature all-PRs merged within chain
- `feature_failed`: Feature dispatch or gate failure
- `chain_advanced`: Successful transition to next feature
- `chain_halted`: Chain stopped, requires intervention
- `chain_completed`: All features done

---

## 8. Contract Boundaries

### 8.1 What This Contract Governs

- Chain state transitions and their conditions
- Feature advancement pre-checks
- Recovery and requeue decisions
- Branch/worktree lifecycle between features
- Carry-forward persistence and visibility

### 8.2 What This Contract Does Not Govern

- Individual dispatch execution (governed by Headless Run Contract)
- Individual review gate behavior (governed by review gate contracts)
- PR merge mechanics (governed by GitHub and CI)
- Dashboard rendering of chain state (implementation concern for PR-1)
- Open item lifecycle within a single feature (governed by existing open_items_manager)

### 8.3 Relationship to Existing Contracts

| Contract | Relationship |
|----------|-------------|
| Headless Run Contract | Chain dispatches individual runs per this contract; chain adds advancement and carry-forward on top |
| Dispatch Guide | Chain uses normal dispatch lifecycle; chain adds inter-feature sequencing |
| Review Gate Evidence Contract | Chain requires gate evidence per existing contract; chain adds cumulative gate tracking |
| Open Items Governance | Chain extends open items with carry-forward semantics; single-feature lifecycle unchanged |

---

## 9. Implementation Notes for Downstream PRs

- **PR-1** (Chain State Projection): Implement chain state as a queryable surface using the states and transitions defined in Sections 2 and 3.
- **PR-2** (Resume/Requeue/Transition): Implement the recovery decision tree (Section 4) and branch/worktree transition sequence (Section 5).
- **PR-3** (Carry-Forward Governance): Implement the carry-forward ledger (Section 6) and verify cumulative persistence.
- **PR-4** (Certification): Execute a real chain using all of the above and certify the contract holds under operational conditions.
