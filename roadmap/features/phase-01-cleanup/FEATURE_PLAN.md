# Feature: Phase 1 — Open Work Cleanup

**Status**: Draft
**Priority**: P0
**Branch**: feature/phase-01-cleanup
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review

Primary objective:
Drain in-flight prior-sprint work so Phase 2 starts with an empty active queue. Operational phase only — no new code is written; existing PRs are merged and queued codex re-audits are replayed once quotas reset.

> **Note on review stacks below:** these waves are *operational* — there is no PR-creating code change to review. The `review_stack` listed is the existing PR's review stack inherited from prior work, not a new gate created by this phase. The phase-end gate is a single closeout review (`gate_phase01_closeout`) confirming all three operational items are evidenced as complete.

## Dependency Flow
```text
W-CL-1 (no deps; blocked on gemini quota recovery)
W-CL-2 (no deps; blocked on gemini quota recovery)
W-CL-3 (no deps; blocked on codex quota recovery 2026-05-05)
W-CL-1, W-CL-2, W-CL-3 -> W-CL-4  (closeout gate)
```

## W-CL-1: Merge PR #395 (ADRs + threshold cleanup script)
**Track**: C
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 hour active (gated on quota recovery)
**Dependencies**: []
**Blocked-On**: gemini_quota_recovery

### Description
Drive PR #395 through gemini review and merge. The PR is already authored and pushed; only the gate + merge remain.

### Scope
- In: re-run gemini gate when quota recovers, verify CI green, merge via `gh pr merge --squash`.
- In: receipt + post-merge projector run (current_state.md auto-updates after Phase 0 lands).
- Out: any new code; no rebases unless conflict appears.

### Files to Create / Modify
- None — operational only.

### Success Criteria
- [ ] gemini gate result shows pass (or accepted advisory) for PR #395
- [ ] CI green on the PR's HEAD
- [ ] PR merged into main with no conflicts
- [ ] Open items linked to PR #395 closed with merge evidence

### Test Plan
**Smoke test:**
- `gh pr view 395 --json state,mergeStateStatus` → state=MERGED, merged after gate result recorded.
- `python3 scripts/build_current_state.py && grep -c '395' .vnx-data/strategy/current_state.md` → PR #395 disappears from "in-flight".

**Coverage target:** N/A (operational).

### Quality Gate
`gate_phase01_w_cl_1_pr395`:
- [ ] PR #395 merged
- [ ] gemini result record exists with non-empty `contract_hash` and `report_path`
- [ ] Linked open items closed with merge SHA evidence

## W-CL-2: Merge PR #396 (UR-001 dead duplicate)
**Track**: C
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 hour active
**Dependencies**: []
**Blocked-On**: gemini_quota_recovery

### Description
Drive PR #396 (UR-001 dead duplicate cleanup) through gemini review and merge. Same closeout flow as W-CL-1.

### Scope
- In: gate run, CI verify, merge.
- Out: any code change.

### Files to Create / Modify
- None — operational only.

### Success Criteria
- [ ] gemini gate result recorded for PR #396
- [ ] CI green
- [ ] PR merged
- [ ] UR-001 duplicate-code references no longer reachable in source tree (`grep` clean)

### Test Plan
**Smoke test:**
- `gh pr view 396 --json state` → MERGED.
- `grep -rn "UR-001 duplicate" scripts/ docs/` → empty.

**Coverage target:** N/A (operational).

### Quality Gate
`gate_phase01_w_cl_2_pr396`:
- [ ] PR #396 merged
- [ ] gemini result record exists with non-empty `contract_hash` and `report_path`
- [ ] No remaining UR-001 duplicate references in tree

## W-CL-3: Batch-replay codex re-audit OIs after May-5 quota reset
**Track**: C
**Priority**: P0
**Complexity**: Low
**Risk**: Medium
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 4 hours batch run + triage
**Dependencies**: []
**Blocked-On**: codex_quota_recovery_2026-05-05

### Description
Run the existing batch-replay script over the ~75 codex re-audit open items queued during quota outage. Each replay either closes the OI (no findings) or reclassifies it as a legitimate work item with a new dispatch.

### Scope
- In: kick off `scripts/replay_codex_reaudits.py` (already exists), monitor failure rate, triage each OI's outcome.
- In: per-OI close-or-reclassify decision logged to `decisions.ndjson` (once Phase 2 lands; until then a markdown summary).
- Out: any change to the replay script itself (treated as black-box for this wave).

### Files to Create / Modify
- None to source — operational only.
- Output artifact: `claudedocs/2026-05-05-codex-reaudit-batch-summary.md` summarizing close/reclassify counts (will move to `docs/audits/` once W-state-3 is live).

### Success Criteria
- [ ] All ~75 OIs replayed (no `pending` left from this batch)
- [ ] Each OI is either `closed` (no findings) or `reclassified` (new dispatch created)
- [ ] Aggregate failure rate documented in batch summary
- [ ] No new blocking findings unaddressed at end of triage

### Test Plan
**Smoke test:**
- `python3 scripts/open_items_manager.py digest | grep -c "codex_reaudit"` → 0 pending after replay.
- `wc -l claudedocs/2026-05-05-codex-reaudit-batch-summary.md` → non-zero (summary exists).

**Coverage target:** N/A (operational).

### Quality Gate
`gate_phase01_w_cl_3_codex_replay`:
- [ ] 0 pending codex_reaudit OIs from the batch
- [ ] Every replayed OI has a terminal state (closed | reclassified)
- [ ] Aggregate batch summary committed

## W-CL-4: Phase 1 closeout verification
**Track**: C
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 hour
**Dependencies**: [W-CL-1, W-CL-2, W-CL-3]

### Description
Operator-visible closeout: verify zero open prior-sprint PRs, zero pending codex re-audit OIs, and no orphaned dispatches. Single verification gate before Phase 2 unblocks. No codex_gate here — Phase 1 produces no source code, so codex review is not applicable; closeout is a checklist gate.

### Scope
- In: read `gh pr list --state open` and confirm no prior-sprint entries remain.
- In: read `open_items_digest.json` and confirm no codex_reaudit pending.
- In: read `.vnx-data/dispatches/active/` and confirm no orphaned dispatch from the cleanup phase.
- Out: any code or config change.

### Files to Create / Modify
- None — operational verification only.

### Success Criteria
- [ ] `gh pr list --state open` returns no prior-sprint PRs (#395 and #396 merged)
- [ ] No codex_reaudit OIs in pending state
- [ ] No orphaned dispatches under `dispatches/active/`
- [ ] `phase_0_complete` precondition flag is now satisfiable for Phase 2 unblock

### Test Plan
**Smoke test:**
- `gh pr list --state open --search "is:pr" | grep -E "#39[56]"` → empty.
- `python3 scripts/open_items_manager.py digest --filter codex_reaudit --state pending` → empty.
- `ls .vnx-data/dispatches/active/` → empty.

**Coverage target:** N/A (operational).

### Quality Gate
`gate_phase01_w_cl_4_closeout` (feature-end gate):
- [ ] Zero open prior-sprint PRs
- [ ] Zero pending codex_reaudit OIs
- [ ] Zero orphaned active dispatches
- [ ] Phase 2 precondition `phase_0_complete + phase_1_complete` is satisfied
