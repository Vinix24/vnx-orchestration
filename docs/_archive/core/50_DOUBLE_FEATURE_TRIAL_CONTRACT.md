# Double-Feature Trial Contract

**Status**: Canonical
**Feature**: Double-Feature Trial Certification
**PR**: PR-0
**Gate**: `gate_pr0_double_feature_trial_contract`
**Date**: 2026-03-31
**Author**: T3 (Track C Architecture)

This document is the single source of truth for what counts as success or failure in the first real double-feature trial. All subsequent PRs (PR-1 through PR-5) evaluate their work against this contract.

---

## 1. Trial Identity

### 1.1 Purpose

Prove that VNX can execute two small features in sequence with:
- real governance (dispatches, receipts, quality gates)
- real branch transitions (merge Feature A, start Feature B on clean main)
- real review-gate evidence (Gemini review, Codex final gate, Claude GitHub optional)
- correct worktree/session continuity (no stale state leaks between features)

### 1.2 Trial Features

| Role | Feature | Branch |
|------|---------|--------|
| Feature A | Inline Stale Lease Reconciliation | Materialized at execution time |
| Feature B | Conversation Resume And Latest-First Timeline | Materialized at execution time |

### 1.3 Trial PR Sequence

| PR | Purpose | Track | Dependencies |
|----|---------|-------|-------------|
| PR-0 | Trial contract, evidence model, invariants | C | None |
| PR-1 | Headless review job contract and evidence receipts | C | None |
| PR-2 | Feature A trial execution and certification | C | PR-0, PR-1 |
| PR-3 | Branch/worktree/auto-next transition validation | C | PR-2 |
| PR-4 | Feature B trial execution and certification | C | PR-3 |
| PR-5 | End-to-end double-feature certification and rollout verdict | C | PR-4 |

---

## 2. Trial Stages

The trial has four stages. Each stage has explicit entry conditions, required evidence, and pass/fail rules. A stage MUST pass before the next stage can begin.

```
Stage 1: Feature A       Stage 2: Transition       Stage 3: Feature B       Stage 4: Certification
Execution & Cert    -->  Validation           -->  Execution & Cert    -->  End-to-End Verdict
(PR-2)                   (PR-3)                    (PR-4)                   (PR-5)
```

Stages are strictly sequential. No stage may begin while a prior stage is incomplete or failed.

---

## 3. Stage 1 — Feature A Execution And Certification

### 3.1 Entry Conditions

- PR-0 (this contract) is merged to the trial branch.
- PR-1 (headless review evidence contract) is merged to the trial branch.
- Feature A plan is activated with a valid FEATURE_PLAN.md.
- Feature A dispatches are created by T0 and promoted through normal governance.

### 3.2 Required Evidence

| # | Evidence | Location | Verification Method |
|---|----------|----------|-------------------|
| A-1 | All Feature A PRs have passing tests | CI / test output | Test command exit code 0, test files exist on disk |
| A-2 | Gemini review gate result exists for each PR where review-stack requires it | `$VNX_STATE_DIR/review_gates/results/` | File exists, `status` field is `pass`, `blocking_count` is 0 |
| A-3 | Gemini review normalized report exists | `$VNX_DATA_DIR/unified_reports/` | File exists at the `report_path` referenced by the gate result |
| A-4 | Codex final gate result exists for each PR where review-stack requires it | `$VNX_STATE_DIR/review_gates/results/` | File exists, `status` field is `pass`, `blocking_count` is 0 |
| A-5 | Codex final gate normalized report exists | `$VNX_DATA_DIR/unified_reports/` | File exists at the `report_path` referenced by the gate result |
| A-6 | Claude GitHub optional gate has explicit state | `$VNX_STATE_DIR/review_gates/results/` | State is `not_configured`, `configured_dry_run`, or `pass` — never silently absent |
| A-7 | Contract hash consistency | Gate request and result files | `contract_hash` in result matches `contract_hash` in request and review contract |
| A-8 | Closure verifier passes for Feature A | Closure verifier output | `verdict: pass` with all checks satisfied |
| A-9 | Feature A residual risks documented | Unified report | Residual risk section present, even if empty |
| A-10 | All Feature A dispatches have receipts | Receipt NDJSON trail | Every dispatch has `task_complete` or `task_failed` receipt entry |

### 3.3 Pass/Fail Rules

**PASS** when ALL of the following hold:
- Evidence A-1 through A-10 exists and is verified.
- No blocking findings remain open in any required review gate.
- Closure verifier returns `verdict: pass`.
- Feature A branch is merged to main.

**FAIL** when ANY of the following hold:
- Any required evidence item is missing.
- Any required review gate has `status: fail` or unresolved blocking findings.
- Closure verifier returns `verdict: fail`.
- Test files claimed in evidence do not exist on disk.
- Gate result `contract_hash` does not match the review contract.
- Report path in gate result points to a file that does not exist.

### 3.4 Failure Recovery

A Stage 1 failure does not terminate the trial. The failure must be:
1. Classified (which evidence item failed and why).
2. Remediated (fix the issue, re-run the failing gate or test).
3. Re-evaluated (re-check all evidence from scratch, not just the fixed item).

Partial re-evaluation is not acceptable. After remediation, all A-1 through A-10 must be verified again.

---

## 4. Stage 2 — Branch, Worktree, And Auto-Next Transition Validation

### 4.1 Entry Conditions

- Stage 1 passed.
- Feature A is merged to main.
- Feature A closure verification is complete.

### 4.2 Required Evidence

| # | Evidence | Location | Verification Method |
|---|----------|----------|-------------------|
| T-1 | Feature A merge commit exists on main | Git history | `git log main` contains the Feature A merge commit |
| T-2 | Feature A branch is no longer the active working branch | Git / worktree state | `git branch --show-current` is not the Feature A branch |
| T-3 | Feature B branch is created from post-merge main | Git history | `git merge-base Feature-B-branch main` equals the merge commit from T-1 |
| T-4 | No stale dispatch state from Feature A in the queue | `$VNX_DATA_DIR/dispatches/` | No pending or staging dispatches referencing Feature A dispatch IDs |
| T-5 | No stale lease state from Feature A | Lease state files | No active leases referencing Feature A dispatch IDs or terminals |
| T-6 | No stale session state from Feature A | Session/worktree state | Worktree for Feature A is removed or archived; no active session references Feature A branch |
| T-7 | Auto-next transition event recorded | Coordination events or state | Event showing Feature A closure triggered Feature B activation |
| T-8 | Feature B plan is activated | FEATURE_PLAN.md or plan state | Feature B has a valid, activated plan with materialized PR queue |

### 4.3 Pass/Fail Rules

**PASS** when ALL of the following hold:
- Evidence T-1 through T-8 exists and is verified.
- Feature B branch is demonstrably based on post-merge main, not on Feature A's branch.
- No stale state from Feature A is detectable in dispatches, leases, sessions, or worktrees.

**FAIL** when ANY of the following hold:
- Feature B branch is based on Feature A's branch instead of main.
- Stale dispatches, leases, or sessions from Feature A are found.
- Feature B plan activation happened before Feature A merge was verified.
- Auto-next transition has no recorded event or audit trail.
- Worktree state references a branch that no longer exists or belongs to Feature A.

### 4.4 Transition Invariants

These invariants are non-negotiable. Any violation is an automatic Stage 2 failure:

| # | Invariant | Rationale |
|---|-----------|-----------|
| TI-1 | Feature B MUST NOT start on Feature A's branch | Prevents code contamination and incorrect merge targets |
| TI-2 | Feature B MUST NOT start before Feature A's merge to main is independently verified | Prevents premature advancement past an unmerged feature |
| TI-3 | Stale state MUST be cleaned or explicitly reconciled, not silently inherited | Prevents ghost dispatches, phantom leases, and incorrect queue ordering |
| TI-4 | The transition MUST be operator-readable in the audit trail | Prevents invisible state transitions that cannot be debugged |

---

## 5. Stage 3 — Feature B Execution And Certification

### 5.1 Entry Conditions

- Stage 2 passed.
- Feature B plan is activated.
- Feature B dispatches are created by T0 and promoted through normal governance.

### 5.2 Required Evidence

Identical structure to Stage 1, but for Feature B:

| # | Evidence | Location | Verification Method |
|---|----------|----------|-------------------|
| B-1 | All Feature B PRs have passing tests | CI / test output | Test command exit code 0, test files exist on disk |
| B-2 | Gemini review gate result exists for each PR where review-stack requires it | `$VNX_STATE_DIR/review_gates/results/` | File exists, `status` field is `pass`, `blocking_count` is 0 |
| B-3 | Gemini review normalized report exists | `$VNX_DATA_DIR/unified_reports/` | File exists at the `report_path` referenced by the gate result |
| B-4 | Codex final gate result exists for each PR where review-stack requires it | `$VNX_STATE_DIR/review_gates/results/` | File exists, `status` field is `pass`, `blocking_count` is 0 |
| B-5 | Codex final gate normalized report exists | `$VNX_DATA_DIR/unified_reports/` | File exists at the `report_path` referenced by the gate result |
| B-6 | Claude GitHub optional gate has explicit state | `$VNX_STATE_DIR/review_gates/results/` | State is `not_configured`, `configured_dry_run`, or `pass` — never silently absent |
| B-7 | Contract hash consistency | Gate request and result files | `contract_hash` in result matches `contract_hash` in request and review contract |
| B-8 | Closure verifier passes for Feature B | Closure verifier output | `verdict: pass` with all checks satisfied |
| B-9 | Feature B residual risks documented | Unified report | Residual risk section present, even if empty |
| B-10 | All Feature B dispatches have receipts | Receipt NDJSON trail | Every dispatch has `task_complete` or `task_failed` receipt entry |

### 5.3 Additional Stage 3 Requirement

Feature B execution MUST demonstrate post-transition correctness:

| # | Evidence | Verification Method |
|---|----------|-------------------|
| B-11 | Feature B tests pass on a codebase that includes Feature A's merged code | Test output shows Feature A code present in the working tree |
| B-12 | Feature B does not regress Feature A functionality | No Feature A tests fail after Feature B changes |

### 5.4 Pass/Fail Rules

**PASS** when ALL of the following hold:
- Evidence B-1 through B-12 exists and is verified.
- No blocking findings remain open in any required review gate.
- Closure verifier returns `verdict: pass`.
- Feature B branch is merged to main.

**FAIL** when ANY of the following hold:
- Any required evidence item is missing.
- Any required review gate has `status: fail` or unresolved blocking findings.
- Closure verifier returns `verdict: fail`.
- Feature B regresses Feature A tests.
- Any evidence from Stage 1 or Stage 2 that was previously valid is now invalid (regression).

### 5.5 Failure Recovery

Same as Stage 1 (Section 3.4). Full re-evaluation of B-1 through B-12 after remediation.

---

## 6. Stage 4 — End-To-End Certification

### 6.1 Entry Conditions

- Stages 1, 2, and 3 all passed.
- Both features are merged to main.
- All review-gate evidence is complete for both features.

### 6.2 Required Evidence

| # | Evidence | Verification Method |
|---|----------|-------------------|
| E-1 | Stage 1 pass evidence is complete and current | Re-verify all A-1 through A-10 are still valid |
| E-2 | Stage 2 pass evidence is complete and current | Re-verify all T-1 through T-8 are still valid |
| E-3 | Stage 3 pass evidence is complete and current | Re-verify all B-1 through B-12 are still valid |
| E-4 | Total review-gate exercise count | Count of Gemini reviews, Codex gates, and Claude GitHub gates actually exercised |
| E-5 | Operator friction assessment | Documented assessment of where governance slowed or blocked legitimate work |
| E-6 | Headless review behavior assessment | Documented assessment of headless review job reliability and evidence quality |
| E-7 | No pseudo-parallelism detected | Audit trail shows no terminal carried two active dispatches simultaneously |
| E-8 | Receipt provenance chain complete | Every dispatch in both features has an unbroken receipt chain |

### 6.3 Certification Verdict

The certification produces exactly one of three verdicts:

| Verdict | Meaning | Criteria |
|---------|---------|----------|
| **GO** | System is ready for broader multi-feature use | All E-1 through E-8 pass. No unresolved blockers. Operator friction is manageable. |
| **CONDITIONAL GO** | System works but has documented limitations | All E-1 through E-8 pass. Unresolved warnings exist but no blockers. Operator friction is high but has identified mitigations. |
| **NO-GO** | System is not ready for broader multi-feature use | Any E-1 through E-8 fails. Unresolved blockers exist. Or operator friction is unacceptable without mitigation. |

### 6.4 Verdict Classification Rules

The verdict MUST separate findings into three categories:

| Category | Definition | Impact on Verdict |
|----------|-----------|-------------------|
| **Blocker** | Prevents correct multi-feature execution | Any blocker → NO-GO |
| **Warning** | Degrades quality or operator experience but does not prevent correctness | Warnings alone → CONDITIONAL GO |
| **Deferred** | Known limitation accepted for later work | Does not affect verdict |

Findings MUST NOT be hidden inside generic "residual risk" text. Each finding is explicitly classified.

---

## 7. Review-Gate Evidence Requirements

### 7.1 Gate Applicability

This trial uses the review stack `gemini_review,codex_gate,claude_github_optional` with `risk_class: high` and `merge_policy: human`.

| Gate | Required | Enforcement |
|------|----------|-------------|
| `gemini_review` | Yes, for every PR with code changes | Result must exist with `status: pass` and `blocking_count: 0` |
| `codex_gate` | Yes, for every PR (high-risk policy) | Result must exist with `status: pass` and `blocking_count: 0` |
| `claude_github_optional` | Explicit state required | Must be `not_configured`, `configured_dry_run`, or `pass` — never silently absent |

### 7.2 Gate Evidence Chain

For each required gate on each PR, the following chain MUST be complete:

```
1. Review contract materialized     → scripts/lib/review_contract.py output
2. Gate request record exists       → $VNX_STATE_DIR/review_gates/requests/{pr_slug}-{gate}-contract.json
3. Normalized markdown report exists → $VNX_DATA_DIR/unified_reports/YYYYMMDD-HHMMSS-HEADLESS-{gate}-{pr-id}.md
4. Structured gate result exists    → $VNX_STATE_DIR/review_gates/results/{pr_slug}-{gate}-contract.json
5. contract_hash matches across 1-4 → SHA256 prefix consistent in contract, request, and result
```

A broken link in this chain blocks closure for that PR.

### 7.3 Gate Result Required Fields

Per the Headless Review Evidence Contract (45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md), every gate result MUST include:

- `gate`, `pr_id`, `branch`, `status`, `summary`
- `contract_hash`, `report_path`
- `blocking_findings` (array), `advisory_findings` (array)
- `blocking_count` (integer), `advisory_count` (integer)
- `required_reruns` (array), `residual_risk` (string)
- `recorded_at` (ISO timestamp)

Missing fields in a gate result are treated as a gate failure.

### 7.4 Contradictory Evidence

If a gate result says `status: pass` but `blocking_count > 0`, the gate is treated as **failed**. The `status` field does not override the blocking findings count.

If a gate request exists but no gate result exists, the gate is treated as **incomplete** and blocks closure.

---

## 8. Branch And Worktree Correctness

### 8.1 Branch Rules

| # | Rule | Verification |
|---|------|-------------|
| BR-1 | Each feature gets its own branch created from main | `git log --oneline main..feature-branch` shows only that feature's commits |
| BR-2 | Feature A branch is merged to main before Feature B branch is created | Merge commit timestamp precedes Feature B branch creation |
| BR-3 | Feature B branch includes Feature A's merged code | `git merge-base main feature-B-branch` is at or after Feature A merge commit |
| BR-4 | No cross-branch contamination | Feature B branch does not contain unmerged Feature A branch commits |

### 8.2 Worktree Rules

| # | Rule | Verification |
|---|------|-------------|
| WT-1 | At most one worktree per feature is active at any time | `git worktree list` shows no overlapping feature worktrees |
| WT-2 | Feature A worktree is removed or archived before Feature B worktree is created | Worktree list at Feature B start does not include Feature A paths |
| WT-3 | Worktree paths do not collide | No two features share a worktree directory |

### 8.3 Session Rules

| # | Rule | Verification |
|---|------|-------------|
| SR-1 | No terminal carries two active dispatches simultaneously | Receipt timestamps show sequential, non-overlapping dispatch execution per terminal |
| SR-2 | Session state from Feature A does not leak into Feature B | No Feature A dispatch IDs appear in Feature B coordination events |
| SR-3 | Interactive Claude sessions use attach/jump/resume semantics | No evidence of terminal injection or raw tmux send-keys for session management |

---

## 9. Non-Goals And Scope Boundary

This trial contract explicitly does NOT cover the following. Any PR work that drifts into these areas is out of scope and must be rejected or deferred.

### 9.1 Explicit Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Rewriting the dispatch state machine | The trial uses the existing dispatch flow. Runtime changes are a separate feature. |
| NG-2 | Rewriting the receipt processor | The trial validates receipt completeness, not receipt architecture. |
| NG-3 | Adding new execution target types | The trial uses existing interactive and headless targets. |
| NG-4 | Changing the review-gate manager architecture | The trial exercises review gates as-is. Gate architecture changes are separate. |
| NG-5 | Implementing new CLI commands for trial orchestration | The trial is validated by T3 inspection, not by new tooling. |
| NG-6 | Parallelizing feature execution | The trial is strictly sequential. Parallel features are a future concern. |
| NG-7 | Changing the closure verifier logic | The trial uses the closure verifier as-is. Verifier changes are separate. |
| NG-8 | Optimizing headless run performance | The trial validates correctness, not performance. |
| NG-9 | Adding new review gate providers | The trial uses gemini_review, codex_gate, and claude_github_optional only. |
| NG-10 | Modifying VNX core infrastructure (.vnx/) | The trial exercises infrastructure, it does not change it. |

### 9.2 Scope Creep Detection

A PR is out of scope if it:
- Modifies files under `.vnx/` (VNX core infrastructure).
- Introduces new database schema beyond what existing contracts require.
- Changes the dispatch state machine transitions.
- Adds new CLI subcommands to `bin/vnx`.
- Modifies the receipt processor event format.
- Changes review-gate manager request/result schemas beyond what the headless review evidence contract already defines.

Out-of-scope changes discovered during the trial are logged as **deferred** findings in the Stage 4 certification, not implemented within the trial.

---

## 10. Failure Modes To Watch

These are known failure modes from prior VNX work. The trial must actively check for them.

| # | Failure Mode | How To Detect | Stage |
|---|-------------|---------------|-------|
| FM-1 | Claimed test files that do not exist | `ls` the claimed test file paths | 1, 3 |
| FM-2 | Claimed totals that do not match actual suite | Run the test command independently and compare counts | 1, 3 |
| FM-3 | Logs that exist but are not linked from receipts | Cross-reference receipt `output_path` with actual log files | 1, 3 |
| FM-4 | Gate result says pass but blocking findings remain | Check `blocking_count > 0` even when `status: pass` | 1, 3 |
| FM-5 | Report path in gate result points to nonexistent file | `ls` the `report_path` value | 1, 3 |
| FM-6 | Feature B started on Feature A branch | Check `git merge-base` and branch parentage | 2 |
| FM-7 | Stale dispatches from Feature A in the queue | Scan dispatch directories for Feature A IDs | 2 |
| FM-8 | Pseudo-parallelism (two dispatches on one terminal) | Check receipt timestamps for overlapping execution windows | 4 |
| FM-9 | Contract hash mismatch between request and result | Compare `contract_hash` fields across request, result, and review contract | 1, 3 |
| FM-10 | Closure claim without merged branch | Check `git branch -r --merged main` for the feature branch | 1, 3 |
| FM-11 | Silent absence of optional gate state | Check that `claude_github_optional` has any explicit state, not just missing | 1, 3 |
| FM-12 | Feature marked complete without operator-run evidence | Verify burn-in or execution evidence is from real runs, not synthetic stubs | 1, 3 |

---

## Appendix A: Evidence Checklist Summary

Quick-reference checklist for each stage. Detailed requirements are in the stage sections above.

### Stage 1 (Feature A)
- [ ] A-1: Tests pass
- [ ] A-2: Gemini review gate result(s) pass
- [ ] A-3: Gemini review report(s) exist
- [ ] A-4: Codex final gate result(s) pass
- [ ] A-5: Codex final gate report(s) exist
- [ ] A-6: Claude GitHub optional has explicit state
- [ ] A-7: Contract hash consistency verified
- [ ] A-8: Closure verifier passes
- [ ] A-9: Residual risks documented
- [ ] A-10: All dispatch receipts complete

### Stage 2 (Transition)
- [ ] T-1: Feature A merge commit on main
- [ ] T-2: Feature A branch no longer active
- [ ] T-3: Feature B branch from post-merge main
- [ ] T-4: No stale dispatch state
- [ ] T-5: No stale lease state
- [ ] T-6: No stale session/worktree state
- [ ] T-7: Auto-next transition event recorded
- [ ] T-8: Feature B plan activated

### Stage 3 (Feature B)
- [ ] B-1: Tests pass
- [ ] B-2: Gemini review gate result(s) pass
- [ ] B-3: Gemini review report(s) exist
- [ ] B-4: Codex final gate result(s) pass
- [ ] B-5: Codex final gate report(s) exist
- [ ] B-6: Claude GitHub optional has explicit state
- [ ] B-7: Contract hash consistency verified
- [ ] B-8: Closure verifier passes
- [ ] B-9: Residual risks documented
- [ ] B-10: All dispatch receipts complete
- [ ] B-11: Feature B tests pass with Feature A code present
- [ ] B-12: No Feature A regression

### Stage 4 (Certification)
- [ ] E-1: Stage 1 evidence still valid
- [ ] E-2: Stage 2 evidence still valid
- [ ] E-3: Stage 3 evidence still valid
- [ ] E-4: Review-gate exercise count documented
- [ ] E-5: Operator friction assessed
- [ ] E-6: Headless review behavior assessed
- [ ] E-7: No pseudo-parallelism detected
- [ ] E-8: Receipt provenance chain complete

## Appendix B: Governance Traceability

| FEATURE_PLAN Invariant | Contract Section |
|----------------------|-----------------|
| Feature B must not begin before Feature A merged | Section 4.4, TI-2 |
| Branch/worktree transition is explicit evidence | Section 4, Section 8 |
| No pseudo-parallelism | Section 8.3, SR-1; Section 6.2, E-7 |
| Headless review jobs return structured receipts | Section 7 |
| Gemini and Codex exercised on real PRs | Section 7.1 |
| Interactive Claude uses attach/jump/resume | Section 8.3, SR-3 |

## Appendix C: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| HEADLESS_RUN_CONTRACT.md | Trial uses this for headless execution evidence format |
| 30_FPC_EXECUTION_CONTRACTS.md | Trial uses task class and routing definitions |
| 45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md | Trial requires gate evidence per this contract |
| review_contract.py | Trial validates contract hash consistency per materializer |
| closure_verifier.py | Trial depends on closure verifier verdict per feature |
