# Feature: Phase 10 — W11 Workers=N Rename And Lease Polymorphism

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w11-workers-n-rename`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Eliminate the hardcoded T1/T2/T3 surface from the codebase. Replace fixed-arity terminal IDs with a polymorphic worker registry where N workers can be active concurrently. The harness keeps backwards compatibility through an alias-fallback layer (T1→worker-001, T2→worker-002 mapping) so existing operator workflows continue to function during rollout. Drives PRD-VNX-UH-001 §FR-9 and unblocks sub-orchestrator pools (W12). Surface inventory documented in claudedocs/2026-05-01-universal-harness-research.md §6 — 25+ files affected.

## Dependency Flow
```text
PR-0 (no dependencies on this feature, depends on W10)
PR-0 -> PR-1
PR-1 -> PR-2
PR-1 -> PR-3
PR-2, PR-3 -> PR-4
PR-1 -> PR-5
PR-4, PR-5 -> PR-6
```

## PR-0: Worker Registry Schema And Storage
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Introduce `worker_registry` table in `runtime_coordination.db` plus parallel JSON view in `.vnx-data/state/workers.json`. Each row: `worker_id` (canonical, e.g. `worker-001`), `kind` (orchestrator | worker | gate-only), `capabilities`, `pool_id`, `provider`, `parent_worker_id`, `lease_status`, `last_heartbeat`. Replaces the implicit T1/T2/T3 enum scattered through the codebase. Designed for OD-6 default-of-8, hard cap of 32 workers per orchestrator.

### Scope
- SQL migration: `worker_registry` table with indices on `pool_id`, `kind`, `parent_worker_id`
- `scripts/lib/worker_registry.py` — CRUD operations, lease query, capability filter
- `.vnx-data/state/workers.json` — read-only mirror for fast operator inspection
- Migration step that seeds three rows: `worker-001`, `worker-002`, `worker-003` (the legacy T1/T2/T3)
- Alias columns: every row has `legacy_alias` (e.g. T1) for backwards compat

### Success Criteria
- Migration applies cleanly on fresh database
- Migration applies cleanly on existing database (idempotent)
- Three legacy rows seeded with correct alias mapping
- Capability filter returns deterministic ordering
- Hard cap 32 workers per pool enforced at insert (configurable via `VNX_WORKER_POOL_CAP`)

### Quality Gate
`gate_pr0_worker_registry`:
- [ ] Migration is idempotent (apply twice → no diff)
- [ ] Three legacy rows present with correct alias
- [ ] Cap enforcement at insert (33rd row → reject)
- [ ] JSON mirror matches DB on every change (consistency test)
- [ ] Indices used by query planner for `pool_id` lookup (EXPLAIN check)

## PR-1: Polymorphic Lease Validation
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Refactor `terminal_leases` table and lease-acquisition logic to be polymorphic: any worker_id can hold a lease, not only T1/T2/T3. Lease-sweep operates on the full registry. Stale-lease cleanup CLI accepts arbitrary worker_id. Critical to get right: lease bugs cause dispatcher block.

### Scope
- Rename column `terminal_id` → `worker_id` in lease tables (with view for backwards compat)
- Update `runtime_core_cli.py check-terminal` to `check-worker` (terminal command kept as alias)
- Update `release-on-failure` command to accept arbitrary worker_id
- Update lease-sweep to iterate `worker_registry` instead of hardcoded list
- Generation-counter logic preserved (existing behavior)

### Success Criteria
- `check-worker --worker worker-005 --dispatch-id <id>` succeeds against arbitrary registered worker
- Lease sweep iterates all registered workers, not just T1/T2/T3
- Backwards-compat alias: `check-terminal --terminal T1` still works (translates to worker-001)
- Generation-counter race-free under concurrent acquire/release

### Quality Gate
`gate_pr1_lease_polymorphism`:
- [ ] Lease acquire/release works for arbitrary registered worker_id
- [ ] Backwards-compat alias for T1/T2/T3 verified
- [ ] Lease sweep covers all registry entries (no hardcoded skip)
- [ ] Generation-counter race test (100 concurrent acquires) deterministic
- [ ] Stale-lease CLI accepts new worker_id format

## PR-2: Naming Rename Batch 1 — Runtime Modules
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [PR-1]

### Description
Mechanical rename across the runtime hot path: dispatcher, smart-tap, receipt-processor, lease-sweep, supervisor, build-t0-state. Replace `terminal_id` → `worker_id` parameter names; replace string literals "T1"/"T2"/"T3" with worker registry lookups. Sonnet appropriate — high-volume mechanical change.

### Scope (per claudedocs/2026-05-01-universal-harness-research.md §6)
- `scripts/dispatcher.py` and dispatcher prelude
- `scripts/smart_tap.py`
- `scripts/receipt_processor.py`
- `scripts/lib/subprocess_dispatch.py`
- `scripts/lib/subprocess_adapter.py`
- `scripts/lib/runtime_supervisor.py`
- `scripts/dispatcher_supervisor.sh`
- `scripts/receipt_processor_supervisor.sh`
- Reconcile-queue-state, lease-sweep, runtime_supervise

### Success Criteria
- All runtime modules reference `worker_id` not `terminal_id` (param name)
- All string literals "T1"/"T2"/"T3" routed through registry lookup or alias layer
- Receipt NDJSON shape unchanged (alias projects back to legacy field for now)
- Dispatcher behavior regression-free against integration tests

### Quality Gate
`gate_pr2_runtime_rename`:
- [ ] Grep for `terminal_id` in runtime modules → only via alias shim
- [ ] Grep for `"T1"|"T2"|"T3"` string literals → only in alias layer
- [ ] Existing dispatcher integration test passes byte-identical receipt
- [ ] Smart tap pickup unchanged

## PR-3: Naming Rename Batch 2 — Tests And Fixtures
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-1]

### Description
Mechanical rename across tests/, fixtures, and helper utilities. Update test parameter names, fixture identifiers, and assertion strings. Preserve any test that intentionally exercises the legacy alias layer (those need both names).

### Scope
- `tests/` — every fixture and helper that uses `terminal_id`
- Mock objects that simulate T1/T2/T3 panes
- Assertion strings checking receipt NDJSON
- Documentation strings inside tests (no behavioral change)

### Success Criteria
- Full test suite passes
- No test references `terminal_id` except in alias-layer regression tests
- Test names readable post-rename (no awkward `test_terminal_id_via_worker_id_T1` naming)

### Quality Gate
`gate_pr3_test_rename`:
- [ ] Full test suite passes
- [ ] Alias-layer regression tests preserved
- [ ] No grep hits for `terminal_id` outside alias layer
- [ ] Test names readable post-rename

## PR-4: Alias-Fallback Layer
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-2, PR-3]

### Description
Centralize all backwards-compat translations in one module: `scripts/lib/worker_alias.py`. Operator UI continues to display T1/T2/T3 but the system internally uses canonical worker_id. CLI commands accept either form. Receipts continue to emit `legacy_alias` field for tooling that has not migrated. Critical to get this layer right — bug here means subtle dispatch routing failures across the harness.

### Scope
- `scripts/lib/worker_alias.py` — `to_canonical(s)`, `to_alias(canonical)`, `display_name(canonical)`
- CLI argument parsing accepts both forms
- Receipt projection: every receipt records both `worker_id` and `legacy_alias`
- Operator-facing strings (state files, docs, log messages) use legacy alias when one exists
- Transition flag: `VNX_WORKER_NAMES=legacy|canonical|both` (default `both`)

### Success Criteria
- `check-terminal --terminal T1` and `check-worker --worker worker-001` both work
- Receipts contain both fields (during transition)
- State file display uses legacy alias for backwards compat with operator memory
- Translation is unambiguous: each canonical id has at most one legacy alias

### Quality Gate
`gate_pr4_alias_layer`:
- [ ] Both CLI forms work for every command (matrix test)
- [ ] Receipts record both fields
- [ ] State files render legacy aliases (operator-readable)
- [ ] Translation table has no collisions (uniqueness test)
- [ ] Transition flag respected: legacy mode uses only T1/T2/T3, canonical mode uses only worker-NNN

## PR-5: Build-t0-state Update
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.5 day
**Dependencies**: [PR-1]

### Description
Update `scripts/build_t0_state.py` to enumerate the worker registry rather than hardcoded T1/T2/T3 sections. State file becomes a list of workers, each with kind, capabilities, lease status, last heartbeat, recent receipts. Operator UX preserved: legacy aliases displayed first.

### Scope
- Replace hardcoded terminal sections with registry-driven enumeration
- Sort: legacy-aliased workers first (T1, T2, T3), then canonical-only
- Each worker section: kind, capabilities, lease, recent receipts (latest 5)
- Pool grouping: workers with same `pool_id` shown together

### Success Criteria
- `t0_state.json` contains every registered worker
- Legacy ordering preserved (T1, T2, T3 appear first)
- Pool grouping renders correctly in JSON
- SessionStart hook still produces fresh state on session start

### Quality Gate
`gate_pr5_build_state`:
- [ ] Every registered worker appears in t0_state.json
- [ ] Legacy aliases ordered first
- [ ] Pool grouping correct
- [ ] SessionStart hook integration unbroken

## PR-6: End-To-End Integration Tests
**Track**: B
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-4, PR-5]

### Description
Full-system integration tests for arbitrary-N workers. Spawn 8 simulated workers, dispatch concurrently, verify lease isolation, receipt routing, and operator state correctness. Includes the full alias-layer test matrix.

### Scope
- 8-worker concurrent dispatch suite
- Pool isolation test (worker in pool A cannot acquire lease for pool B's work)
- Alias-layer regression matrix (every CLI command × both forms)
- Backwards-compat soak: 30-minute run with mixed legacy and canonical IDs

### Success Criteria
- 8 concurrent workers dispatch and complete without lease collisions
- Pool isolation enforced
- Every CLI command works with both forms
- 30-minute soak run produces no false-positive lease conflicts

### Quality Gate
`gate_pr6_e2e_workers_n`:
- [ ] 8 concurrent dispatches complete without lease collision
- [ ] Pool isolation: cross-pool lease attempt rejected
- [ ] CLI matrix: every command × both name forms passes
- [ ] 30-minute soak run: zero false lease conflicts
- [ ] CODEX GATE on this PR is mandatory feature-end gate
- [ ] CLAUDE_GITHUB_OPTIONAL on this PR is mandatory triple-gate (architecture-critical, lease bugs cause dispatcher deadlock)

## Test Plan (Phase-Level)

### Schema Tests
- Migration apply twice → no diff
- Cap enforcement (33rd row in pool → reject)
- Index usage check via EXPLAIN
- JSON mirror consistency under concurrent writes

### Lease Tests
- Acquire / release for arbitrary worker_id
- Generation-counter race (100 concurrent acquires, exactly one wins)
- Stale-lease sweep covers full registry
- Backwards-compat alias verified

### Rename Coverage Tests
- Grep audit: no `terminal_id` outside alias layer
- Grep audit: no string literals "T1|T2|T3" outside alias layer
- Receipt schema unchanged (alias-projected during transition)

### Alias Layer Tests
- `to_canonical("T1")` → `"worker-001"`
- `to_alias("worker-001")` → `"T1"`
- `to_alias("worker-005")` → `None` (no legacy alias)
- Round-trip: canonical → alias → canonical (when alias exists)
- Transition flag respected (legacy / canonical / both)

### Concurrent Workers Tests
- 8 workers dispatch concurrently → no lease collision
- 32 workers (cap) dispatch → all complete
- 33rd worker → registration rejected with cap-enforced error

### Pool Isolation Tests
- Workers in pool A cannot pick up dispatches addressed to pool B
- Cap is per-pool, not global (validated with 2 pools × 32 workers each)

### Operator UX Regression
- t0_state.json renders legacy aliases first
- SessionStart hook produces fresh state
- Operator-facing log messages still use T1/T2/T3 in legacy mode
- All `.claude/terminals/T0-T3/CLAUDE.md` files still resolved correctly

### Backwards-Compat Soak
- 30-minute run with mixed legacy/canonical CLI invocations → no leaks, no false-positive lease conflicts, no receipt drift
