# Feature: Phase 15 / W15 — VNX Roadmap Autopilot, Auto-Next Feature Loading, And Multi-Reviewer Gates

**Status**: Draft
**Priority**: P0
**Branch**: `feature/roadmap-autopilot-review-gates`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Roadmap Wave**: w15-pr0..w15-pr3 (4 waves)
**Depends-On (roadmap)**: w14 (folder-agent cutover; required so the autopilot operates over the universal harness, not the legacy injection path)

> This file is an **extended copy** of `roadmap/features/roadmap-autopilot/FEATURE_PLAN.md`. The original (PR-0..PR-3) was format-reference for the rest of the roadmap; this version keeps the structure and adds detailed Test Plan sections per sub-PR. When the two files disagree, **this file wins** — the original is preserved as historical reference.

Primary objective:
Enable multi-feature roadmap orchestration with automatic feature handoff after merged + verified closure. After this wave lands, T0 self-orchestrates: a feature merging triggers reconciliation of `roadmap.yaml`, advancement to the next feature whose dependencies are now met, and dispatch of that feature's first PR — without operator intervention.

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
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Estimated LOC**: ~400
**Dependencies**: []

### Description
Introduce the roadmap registry, active feature materialization, and roadmap state tracking. Reads `.vnx-data/strategy/roadmap.yaml` (the canonical source landed in Phase 0/W-UX-1). Materializes the *active* feature into root `FEATURE_PLAN.md` + `PR_QUEUE.md` (only one feature is "active" at a time; the rest live under `roadmap/features/<phase>/`).

### Scope
- `scripts/lib/roadmap_registry.py`: schema-validated reader/writer over `roadmap.yaml`
- `scripts/lib/feature_materialization.py`: hydrates the active feature's `FEATURE_PLAN.md` and `PR_QUEUE.md` at repo root from `roadmap/features/<active-id>/`
- `.vnx-data/state/roadmap_state.json`: rolling state file (current_active, last_advanced_at, blocked_on)
- Idempotency: materializing the same feature twice is a no-op; materializing a different feature first archives the previous root files

### Success Criteria
- Roadmap can initialize and load one active feature
- Root `FEATURE_PLAN.md` and `PR_QUEUE.md` represent only the active feature
- `roadmap_state.json` reflects the active feature consistently after every operation

### Test Plan
- **Schema test**: a malformed `roadmap.yaml` (missing `phase_id`, duplicate `wave_id`, dependency cycle) is rejected with a clear pydantic-style error; no partial state is written
- **Init-from-valid-yaml test**: a known-good `roadmap.yaml` initializes the registry; `roadmap_state.json` contents match a fixture
- **Idempotency test**: running `init` twice produces identical state; root `FEATURE_PLAN.md` checksum unchanged on second run
- **Materialization swap test**: switching the active feature archives the previous root files under `roadmap/features/<old-id>/.archive/<timestamp>/` (so we never destroy operator history)
- **Dependency-cycle rejection test**: a yaml with `A depends on B; B depends on A` fails fast with `cycle_detected` error
- **Concurrent-write safety test**: two parallel calls to materialize race against each other; one wins, the other detects the lock and exits cleanly (filesystem-level lock, no SQLite needed for this surface)
- **State-shape regression test**: `roadmap_state.json` schema is locked behind a fixture; field additions require a migration entry
- **End-to-end test**: from a fresh checkout, `python -c "from scripts.lib.roadmap_registry import init; init()"` produces the same root FEATURE_PLAN.md content as `roadmap/features/<phase-13>/FEATURE_PLAN.md` (assuming Phase 13 is the active feature for this fixture)

### Quality Gate
`gate_pr0_roadmap_registry`:
- [ ] Roadmap registry initializes cleanly from a valid yaml
- [ ] Malformed yaml is rejected without partial writes
- [ ] Feature materialization is deterministic (idempotent)
- [ ] Cycle detection green
- [ ] Concurrent-write safety green
- [ ] gemini_review green
- [ ] codex_gate green (PR-level, since this is foundation)

## PR-1: Review Gate Stack (Formalized)
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Estimated LOC**: ~250
**Dependencies**: [PR-0]

### Description
Add Gemini, Codex, and optional Claude GitHub review gate adapters as a unified review gate stack. Each feature's `review_stack` field (already in `roadmap.yaml`) drives which gates are required for that feature. Review requests and results are tracked durably under `.vnx-data/state/review_gates/{requests,results}/` (already partially exists; formalize the schema and the closure-evidence contract).

### Scope
- `scripts/lib/review_gate_stack.py`: single entrypoint to request a gate, persist the request, dispatch to the gate runner, persist the result, link the result to the active review contract via `contract_hash`
- Result record schema: `{gate_id, feature_id, pr_number, contract_hash, report_path, status, evidence: {...}}`
- Optional path: `claude_github_optional` cleanly skips when not configured (env var or feature config), but logs the skip explicitly so it never silently disappears
- Receipt emission per review-gate event

### Success Criteria
- Review requests and results are tracked durably under `.vnx-data/state/review_gates/`
- Optional `claude_github_optional` path skips cleanly when not configured AND records the skip
- T0's existing closure verifier (PR-2 below) can read these records via a stable interface

### Test Plan
- **Stack-population test**: a feature whose `review_stack: [gemini_review, codex_gate]` triggers exactly two requests; a feature with `review_stack: [gemini_review, codex_gate, claude_github_optional]` triggers two or three depending on configuration
- **Required-gate failure test**: gemini gate returns failure -> review stack reports `failed`, blocking-finding propagates upstream
- **Optional-gate skip test**: `claude_github_optional` with env unset records a `skipped` result with `reason=not_configured` (NOT silent absence)
- **Optional-gate present test**: `claude_github_optional` with env set executes and records the result alongside the others
- **Contract-hash linking test**: every result record's `contract_hash` matches the active contract; a mismatched hash invalidates the result and re-requests the gate
- **Empty-evidence rejection test**: a result record with empty `contract_hash` or empty `report_path` is treated as evidence failure (per T0 CLAUDE.md rule)
- **Receipt-emission test**: every gate request and result emits a structured governance receipt visible in `t0_receipts.ndjson`
- **Schema-stability test**: result-record schema is fixture-locked; field changes require a migration
- **Concurrency test**: two parallel review-gate requests for the same PR don't corrupt the request directory (atomic write or per-gate filename)
- **Headless report test**: a normalized headless report exists under `$VNX_DATA_DIR/unified_reports/headless/` for every gate that requires one

### Quality Gate
`gate_pr1_review_stack`:
- [ ] Review requests are emitted deterministically
- [ ] Result recording produces durable evidence
- [ ] Optional path skip logged, never silent
- [ ] `contract_hash` linking enforced
- [ ] Empty-evidence rejection green
- [ ] Schema fixture-locked
- [ ] gemini_review green
- [ ] codex_gate green

## PR-2: Closure Verifier And Auto-Merge Policy
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Estimated LOC**: ~350
**Dependencies**: [PR-0]

### Description
Add executable closure verification and conditional auto-merge policy evaluation. T0's "closure-ready" claim becomes a function call, not a vibe. Auto-merge is policy-evaluable but defaults OFF: low-risk, well-evidenced features may auto-merge; high-risk features always require human gate per `merge_policy: human`.

**Why Opus (existing plan said Opus; reaffirmed):** auto-merge is meta-governance and security-sensitive. A bug here can cascade-merge a broken feature.

### Scope
- `scripts/lib/closure_verifier.py`: executable check that runs the contract for a PR (review stack complete, CI green via `gh`, no blocking open items, no contradictions between result JSON and normalized report)
- Metadata sync checks: feature's `roadmap.yaml` status matches actual PR/merge state
- GitHub merge-state verification via `gh pr view --json`
- `scripts/lib/auto_merge_policy.py`: policy evaluator. Inputs: closure-verifier result + feature's risk class + merge_policy. Outputs: `auto_merge_allowed` boolean + reason string
- Receipt emission per verification step

### Success Criteria
- T0 closure-ready claims become executable: `closure_verifier.verify(feature_id, pr_number)` returns structured pass/fail
- Low-risk conditional auto-merge is policy-evaluable; high-risk paths are blocked at the policy level
- Closure verifier blocks inconsistent merge claims (e.g., result JSON says pass, normalized report says fail)

### Test Plan
- **Auto-merge fires-only-on-all-pass test**: auto_merge_policy returns `allowed=True` only when ALL of: codex_gate=passed, gemini_review=passed (or `skipped` per stack), CI=green via `gh`, zero blocking findings, merge_policy != human, risk_class in {low, medium}
- **Auto-merge blocks high-risk test**: risk_class=high -> `allowed=False, reason="risk_class_high_requires_human"`
- **Auto-merge blocks merge_policy=human test**: even on low risk, `merge_policy=human` -> `allowed=False, reason="explicit_human_gate"`
- **Closure contradiction detection test**: result JSON says `passed`, normalized report contains `BLOCKING:` markers -> verifier returns `inconsistent_evidence` with both pointers
- **CI-green test**: `gh pr checks <num>` shows red -> verifier returns `ci_red`
- **Empty-contract-hash test**: a result record with empty `contract_hash` -> verifier returns `evidence_incomplete` (per CLAUDE.md rule)
- **Empty-report-path test**: a result record with empty `report_path` -> verifier returns `evidence_incomplete`
- **Queued-only test**: a required gate in `queued` state is treated as incomplete; closure denied
- **Metadata-sync test**: `roadmap.yaml` says `in_progress` but PR is merged -> verifier returns `metadata_drift` and exposes the drift to PR-3 (which decides whether to fix-up or just sync)
- **Receipt audit test**: every verifier call produces a receipt with the structured pass/fail
- **Property-based test (hypothesis)**: 1000 random combinations of gate-states + risk + policy, assert that auto-merge ONLY fires under the documented all-pass conjunction
- **Security test**: an attacker who flips a result JSON's `status` field but leaves the normalized report unchanged is detected by the contradiction check

### Quality Gate
`gate_pr2_closure_policy`:
- [ ] Closure verifier blocks inconsistent merge claims
- [ ] Auto-merge policy blocks high-risk paths
- [ ] Property-based test green (1000 cases)
- [ ] Security test green (tamper detected)
- [ ] Metadata-sync drift surfaced cleanly to PR-3
- [ ] gemini_review green
- [ ] codex_gate green

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
**Estimated LOC**: ~450
**Dependencies**: [PR-1, PR-2]

### Description
Advance the roadmap only after merged + verified closure, and insert blocking fix-up features when drift is detected. This is the meta-orchestration capstone: when feature A's closure is verified, the autopilot reconciles `roadmap.yaml`, picks the next feature whose dependencies are now met (feature B), materializes feature B's plan into the root files, and dispatches feature B's first PR.

If the closure verifier reports drift (e.g., metadata says `in_progress` but no PR exists; or a feature was merged but `roadmap.yaml` was never updated), the autopilot inserts a fix-up feature *ahead* of the next planned feature rather than silently advancing.

**Why Opus (existing plan said Opus; reaffirmed):** highest meta-complexity in the roadmap. A regression here can cascade-fail every future feature.

### Scope
- `scripts/lib/roadmap_reconcile.py`: reconciles `roadmap.yaml` with actual merge state, surfaces drifts
- `scripts/lib/roadmap_advance.py`: picks the next feature whose `depends_on` are all `completed`, materializes it, dispatches its first PR
- `scripts/lib/fixup_insertion.py`: when drift is blocking (e.g., merged feature broke schema for a downstream wave), inserts a synthetic `fix-up-<source-feature>` feature into `roadmap.yaml` ahead of the next-planned feature, with `depends_on` pointing back at the drift evidence
- Hook wiring: post-merge hook (or operator command `vnx autopilot tick`) triggers reconcile + advance

### Success Criteria
- Next feature loads automatically only after verified merge (closure verifier from PR-2 says pass)
- Blocking drift inserts a fix-up before roadmap advancement
- The fix-up feature's `FEATURE_PLAN.md` is auto-stubbed with the drift evidence as scope

### Test Plan
- **End-to-end mission test (the load-bearing test)**: feature A completes (merge + verified closure) -> `roadmap.yaml` status flips to `completed` -> feature B (whose deps are now met) is auto-loaded -> feature B's first PR is auto-dispatched. Verify the entire flow without operator intervention. Assert: receipts trail shows the chain; `roadmap_state.json` reflects feature B as active; root `FEATURE_PLAN.md` matches `roadmap/features/<feature-B>/FEATURE_PLAN.md`
- **No-advance-on-pending-merge test**: feature A's PR is open but unmerged -> reconcile reports `awaiting_merge`, no advance
- **No-advance-on-failed-closure test**: feature A merged but closure verifier reports `inconsistent_evidence` -> no advance, blocking OI raised
- **Drift-detection test**: simulate merged feature without `roadmap.yaml` status flip -> reconcile detects `metadata_drift_post_merge`, inserts fix-up feature with auto-stubbed plan
- **Fix-up insertion ordering test**: fix-up is inserted *ahead* of the next planned feature, not appended to the end
- **No-deps-met test**: when no feature has all deps met (e.g., only blocked features remain), advance returns `no_advance, reason="no_eligible_feature"` and emits a structured OI for operator
- **Idempotency test**: running `tick` twice in a row produces no-op on the second run
- **Crash-recovery test**: kill the advance script mid-materialization -> next run detects partial state and recovers (rolls back partial materialization or completes it deterministically)
- **Auto-merge integration test**: with PR-2's auto-merge enabled and risk_class=low feature, the full chain auto-merges + auto-advances + auto-dispatches without operator
- **Auto-merge disabled high-risk test**: with risk_class=high, auto-merge denies, advance still waits for human merge before progressing
- **Receipt audit test**: every step in the chain (merge detection, closure verification, reconcile, advance, fix-up insertion, dispatch) emits a receipt
- **claude_github_optional gate**: invoked at PR level because this is the meta-orchestration feature
- **Codex gate (feature-end)**: zero blocking findings on auto-orchestration semantics

### Quality Gate
`gate_pr3_auto_advance`:
- [ ] End-to-end mission test green (the load-bearing test)
- [ ] Auto-next only occurs after merged + verified closure
- [ ] Blocking drift produces a fix-up feature instead of silent advancement
- [ ] Crash-recovery test green
- [ ] Idempotency test green
- [ ] gemini_review green
- [ ] codex_gate green
- [ ] claude_github_optional executed; result recorded

## Model Assignment Justification

| PR | Model | Rationale (vs existing plan) |
|----|-------|------------------------------|
| PR-0 registry + materialization | Opus | Existing plan: Opus. Kept. Schema-validated reader, idempotent state machine, concurrency safety — all benefit from Opus. |
| PR-1 review gate stack | Sonnet | Existing plan: Sonnet. Kept. Mostly plumbing over an existing surface. |
| PR-2 closure verifier + auto-merge | Opus | Auto-merge is meta-governance, security-sensitive. Property-based testing + tamper-detection require careful reasoning about the closure contract. |
| PR-3 auto-advance + drift fix-up | Opus | Highest meta-complexity. End-to-end mission orchestration without operator. A regression cascade-fails every future feature. |

## Wave-End Quality Gate

`gate_w15_feature_end`:
- [ ] All 4 PR gates green (PR-0, PR-1, PR-2, PR-3)
- [ ] codex_gate (feature-end) green
- [ ] claude_github_optional executed on PR-3 (meta-orchestration blast radius)
- [ ] End-to-end mission test green
- [ ] Drift-fix-up insertion verified
- [ ] Receipts trail covers the full auto-advance chain
- [ ] `roadmap_state.json` schema fixture-locked

## Phase 14 -> Phase 15 Handoff (Why Phase 14 Must Land First)

Phase 15 self-orchestrates by reading `roadmap.yaml`, materializing one active feature, dispatching its PRs, and advancing on verified closure. Every dispatch the autopilot creates is delivered through the **dispatcher's main path**. If that path still contains the legacy `_inject_skill_context()` branch from pre-W14, the autopilot is partly orchestrating over a path that is scheduled for deletion — meaning every autopilot-issued dispatch carries dual-path uncertainty (folder-agent or legacy-injection? depends on env at the time of dispatch).

By landing W14 first:
- The dispatcher has exactly one path (folder-agents)
- The autopilot's dispatch shape is stable and matches what the universal harness uses
- Receipts produced by autopilot-issued dispatches conform to the post-W14 schema (no `legacy_inject_path` field), so PR-2's closure verifier doesn't have to handle two receipt shapes
- PR-3's auto-advance can rely on the deprecation warnings being absent (clean dispatches), making the receipts trail audit-clean

If Phase 15 landed before Phase 14, every autopilot-issued dispatch would risk routing through the soon-to-be-deleted legacy path, the closure verifier would need dual-shape support, and the W14 cutover would later require auditing every autopilot-emitted receipt to confirm path. That is unnecessary risk, so Phase 14 is the gating dependency.

## Notes / Risks

- **Mandatory triple gate per PR** (per memory): every PR in this wave goes through gemini -> codex -> CI green -> merge. No skipping
- **Auto-merge defaults OFF** for high-risk features; PR-2 enforces this at the policy level
- **Fix-up insertion is conservative**: prefer inserting a fix-up over silently advancing past drift
- **End-to-end mission test is non-negotiable** — it is the proof that the autopilot is real, not vibes
