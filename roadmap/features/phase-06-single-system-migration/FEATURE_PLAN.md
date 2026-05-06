# Feature: Single-VNX Migration P1-P6 — Federation -> Identity -> Envelopes -> Data Import -> Reader Cutover -> DB Retirement

**Status**: Draft
**Priority**: P0
**Branch**: `feature/phase-06-single-system-migration`
**Risk-Class**: critical
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Consolidate 4 isolated VNX deployments (`vnx-roadmap-autopilot`, `mission-control`, `sales-copilot`, `SEOcrawler_v2`) into ONE central VNX install at `~/.vnx-system/` with ONE central `~/.vnx-data/` namespaced by `project_id`, while preserving per-project git/CI/secrets boundaries. Phase 0 (`project_id` schema columns + indexes) already shipped via PRs #334 + #358 — this feature plan covers Phases 1–6 of the migration plan in `claudedocs/2026-04-30-single-vnx-migration-plan.md`.

Reference plan: `claudedocs/2026-04-30-single-vnx-migration-plan.md` (~6500 words, full per-phase risk analysis). The 6 sub-PRs below correspond 1-to-1 with Phases 1 through 6 of that plan.

## Dependency Flow
```text
w6-p1 (no dependencies — read-only aggregator stands alone)
w6-p1 -> w6-p2
w6-p2 -> w6-p3
w6-p3 -> w6-p4   (HIGHEST RISK — one-shot data import)
w6-p4 -> w6-p5
w6-p5 -> w6-p6   (cleanup; depends on >=7 days stable Phase 5 operation)
```

## Risk-Mitigation Note (applies to entire feature)

This is the single highest-risk feature in the entire roadmap. Two mitigation properties are non-negotiable:

1. **Additive-only through w6-p4.** No reads change semantics until w6-p5 (reader cutover). Until then, every existing call site continues reading per-project DBs unchanged; central DB merely accumulates a parallel copy.
2. **Reversible through w6-p5.** Per-project DBs are retained until 7 days after w6-p5 ships green — only w6-p6 retires them. A rollback at any point through w6-p5 is `set VNX_USE_CENTRAL_DB=0` plus restore `.vnx -> .vnx.bak/` symlinks. Zero project data is lost.

## w6-p1: Read-Only Federation Aggregator
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: Low
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 week (week 2–3 of 9-week timeline; ~300 LOC after deferring FastAPI dashboard to a stretch goal)
**Dependencies**: []

**Model justification (Opus):** Despite being read-only, this PR makes the foundational cross-project schema decisions (which DBs attach in which order, how project_id is synthesized for legacy rows, what the unified view shape is) that Phases 2–6 build on. Schema decisions made wrong here force expensive rework in w6-p2 and w6-p4. The plan's default-Sonnet recommendation for read-only PRs is overridden because of the cross-project blast radius.

### Description
Stand up a read-only federation aggregator at `~/.vnx-aggregator/` that periodically attaches to all 4 projects' `quality_intelligence.db`, `runtime_coordination.db`, and `dispatch_tracker.db` files in `?mode=ro` mode. Materialize a unified read-only view DB at `~/.vnx-aggregator/data.db` and expose a tiny FastAPI + HTML dashboard at `localhost:8910` showing each project's active dispatches, latest receipts, recent patterns, WAL sizes. Aggregator is the operator's first concrete "see all 4 projects in one place" deliverable and validates schema-drift claims of `claudedocs/2026-04-30-single-vnx-migration-plan.md` §0.1 / §4.3 before any write migration is attempted.

### Scope
- Aggregator service (`scripts/aggregator/build_central_view.py`, refresh loop, read-only attachment)
- Aggregator dashboard (FastAPI + HTML at `localhost:8910`)
- launchd plist (`~/Library/LaunchAgents/com.vnx.aggregator.plist`) running every 60s
- Project registry expansion: `~/.vnx/projects.json` from 1 project to 4 projects (schema_version stays at 1; w6-p2 bumps to 2)
- Schema-drift report tool: prints per-project table list and column diffs (used as preflight by w6-p4)

### Files to Create/Modify
- **Create:** `scripts/aggregator/build_central_view.py` (~140 LOC) — attaches all 4 source DBs, materializes views in `~/.vnx-aggregator/data.db`
- **Create:** `scripts/aggregator/aggregator_dashboard.py` (~80 LOC) — FastAPI app, single endpoint `/`, simple HTML template
- **Create:** `scripts/aggregator/refresh_loop.sh` (~25 LOC) — bash wrapper called by launchd
- **Create:** `scripts/aggregator/templates/index.html` (~40 LOC) — minimalist counts table + WAL-size column
- **Create:** `scripts/aggregator/schema_drift_report.py` (~30 LOC) — preflight tool used by w6-p4
- **Modify:** `~/.vnx/projects.json` (registry expanded from 1 to 4 entries; this is operator-config, not in-repo)

### Success Criteria
- Aggregator daemon runs continuously without holding write locks on any source DB
- `localhost:8910` displays one row per project: dispatch counts, recent receipt count, last activity timestamp, current WAL size
- Schema-drift report identifies missing tables in `sales-copilot` (`cost_per_dispatch`, `dispatch_metadata`, `intelligence_effectiveness`, `dispatch_pattern_offered` — per plan §4.3)
- `~/.vnx/projects.json` lists all 4 projects with correct paths and proposed `project_id` values

### Test Plan
- **Unit:**
  - `test_build_central_view.py` — given 4 fixture DBs, assert views materialize with correct row counts and project_id namespacing
  - `test_schema_drift_report.py` — assert report flags expected drift between fixtures
  - `test_aggregator_readonly.py` — assert that running the aggregator against a fixture DB does not change its mtime or WAL size
- **Integration:**
  - Spin up aggregator against the 4 real `.vnx-data/state/` paths in CI fixtures; assert dashboard returns 200 and the counts table contains 4 rows
  - Assert refresh loop runs idempotently — running 3 times in a row produces identical view-DB content (modulo `materialized_at` timestamp)
- **Smoke:**
  - `curl -fsS localhost:8910/` returns HTML containing the four project_ids
  - `python3 scripts/aggregator/schema_drift_report.py --json` exits 0 and emits valid JSON
- **Dry-run mode test:** `build_central_view.py --dry-run` walks all attach/select logic without writing to `~/.vnx-aggregator/data.db`; assert exit 0 and stdout includes "DRY-RUN: would write N rows" for each project.

### Quality Gate
`gate_w6_p1_federation_aggregator`:
- [ ] Aggregator runs read-only — no source-DB mtime/WAL changes detected after 1-hour soak
- [ ] All 4 projects appear in dashboard with non-zero last-activity timestamps (where source DB is non-empty)
- [ ] Schema-drift report matches plan §4.3 ground truth (missing-table list correct)
- [ ] launchd plist installs and starts; aggregator survives a restart of the laptop
- [ ] `~/.vnx/projects.json` validates against schema_v1 JSON schema check
- [ ] No regression on per-project workflows — running an active dispatch on `vnx-roadmap-autopilot` succeeds while aggregator is up
- [ ] Dry-run mode produces no filesystem mutations (verified via `find ~/.vnx-aggregator -newer <ts>`)

## w6-p2: Identity Layer + Per-Project Worker Registry
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 week (week 4 of 9-week timeline; ~250 LOC + ~70 LOC bulk-update scanner)
**Dependencies**: [w6-p1]

**Model justification (Opus):** This PR touches `append_receipt.py` (1255 LOC, fcntl-locked, hot path on every dispatch) and `subprocess_dispatch.py` (worker spawn). One missed call site = one cross-project pollution event that cannot be un-attributed retroactively (plan §7.2). Opus is required for the careful additive-only review of every writer.

### Description
Implement the four-tuple identity model `{operator}/{project}/{orchestrator}/{agent}` end-to-end. Every script that touches SQLite or NDJSON resolves identity via `scripts/lib/vnx_identity.py` at startup and propagates it. Project registry at `~/.vnx/projects.json` bumps to schema_version 2 with full operator/agents sections. Each project gets a `.vnx-project-id` file at git-root (3 lines: project_id, orchestrator_id, agent_id; last two optional). Identity flows through subprocess dispatch via env vars (`VNX_OPERATOR_ID`, `VNX_PROJECT_ID`, `VNX_ORCHESTRATOR_ID`, `VNX_AGENT_ID`); spawned `claude -p` workers read these in their own `resolve_identity()` call.

### Scope
- New helper `scripts/lib/vnx_identity.py` with `VnxIdentity` dataclass + `resolve_identity()` resolution chain (env -> .vnx-project-id file -> registry lookup -> RuntimeError)
- Registry schema bump (`~/.vnx/projects.json` schema_version 1 -> 2) including `operator_id` and `agents` sections
- `.vnx-project-id` file at each project's git-root (4 new files; 1 in this repo, 3 added during w6-p5 for the other projects)
- Identity propagation through `subprocess_dispatch.py` env vars
- Identity stamping in `append_receipt.py` and `dispatch_register.py` (additive — new optional fields with default `None`)
- Strict ID format: regex `^[a-z][a-z0-9-]{1,31}$`; reserved id `_unknown` for migration-only
- Bulk-update scanner `scripts/migrate_phase2_identity.py` that finds all SQLite-touching scripts and lints for env var inheritance

### Files to Create/Modify
- **Create:** `scripts/lib/vnx_identity.py` (~120 LOC)
- **Create:** `scripts/migrate_phase2_identity.py` (~70 LOC; bulk scanner + lint)
- **Create:** `.vnx-project-id` at repo root (3 lines: `vnx-dev`, `dev-T0`, blank)
- **Modify:** `~/.vnx/projects.json` (schema_v1 -> schema_v2; +`operator_id`, +`agents` sections)
- **Modify:** `scripts/append_receipt.py` (+30 LOC; additive identity stamping; default kwargs preserve backward compat)
- **Modify:** `scripts/lib/dispatch_register.py` (+20 LOC; same pattern)
- **Modify:** `scripts/lib/subprocess_dispatch.py` (+15 LOC; propagate identity env vars into worker spawn)
- **Modify:** `scripts/build_t0_state.py` (+10 LOC; stamp identity into output JSON)
- **Modify:** `scripts/lib/project_root.py` (+15 LOC; extend `resolve_data_dir` with optional `project_id` param)

### Cross-project isolation test (per plan §7.1, §7.2)
After the identity layer lands, spawn workers in 2 simulated projects (fixture project A with `VNX_PROJECT_ID=vnx-dev` and fixture project B with `VNX_PROJECT_ID=mc`). Both write a receipt and a register event in the same second. Assert:
- Each receipt has its own `project_id` field set correctly
- A read against `WHERE project_id='vnx-dev'` returns ONLY project A's rows
- A read against `WHERE project_id='mc'` returns ONLY project B's rows
- Receipts NDJSON archives are kept on separate paths (per-project dirs already exist in archive layout)

### Success Criteria
- Every script that writes SQLite or NDJSON calls `resolve_identity()` once and propagates the four-tuple
- A dispatch on `vnx-roadmap-autopilot` writes receipts with `project_id='vnx-dev'`, `orchestrator_id='dev-T0'`, `agent_id='T1'` (or whichever terminal)
- Cross-project isolation test passes with zero pollution
- `vnx_identity.refuse_unknown=True` post-Phase 2: scripts that cannot resolve a project_id refuse to run (RuntimeError)

### Test Plan
- **Unit:**
  - `test_vnx_identity_resolve_env.py` — env vars present -> returns four-tuple from env
  - `test_vnx_identity_resolve_file.py` — `.vnx-project-id` file present -> returns project_id from file, defaults from registry
  - `test_vnx_identity_resolve_registry.py` — fall back to git-root path lookup
  - `test_vnx_identity_refuse.py` — no resolution path -> raises RuntimeError
  - `test_vnx_identity_id_format.py` — invalid IDs (uppercase, too long, reserved `_unknown` outside migration mode) raise ValueError
  - `test_append_receipt_backward_compat.py` — call without identity kwargs -> receipt still lands; just lacks new fields (per NFR-9 of PRD)
- **Integration:**
  - End-to-end dispatch on T1 — receipt arrives in `t0_receipts.ndjson` with `project_id='vnx-dev'` populated
  - `subprocess_dispatch.py` worker — env vars propagate; worker's own `resolve_identity()` returns the parent's four-tuple
  - Cross-project isolation test (described above)
- **Smoke:**
  - `python3 scripts/migrate_phase2_identity.py --check` exits 0 (lint clean across all SQLite-touching scripts)
  - `cat .vnx-project-id` returns 3 lines starting with `vnx-dev`
- **Dry-run mode test:** `migrate_phase2_identity.py --dry-run` reports which scripts would be edited; assert no actual file writes occur.
- **Backward-compat test:** all existing tests in `tests/runtime/`, `tests/governance/`, `tests/dispatch/` pass unchanged. Identity fields are additive; default values don't break old readers.

### Quality Gate
`gate_w6_p2_identity_layer`:
- [ ] `resolve_identity()` returns the correct four-tuple in all 4 resolution paths
- [ ] No script writes a row without `project_id` populated (audited via SQL trigger or post-write select)
- [ ] Cross-project isolation test passes — zero pollution between fixtures
- [ ] `append_receipt.py` regression suite green (1255 LOC hot path; this is the biggest single risk in the PR)
- [ ] Subprocess workers inherit the four-tuple correctly
- [ ] `~/.vnx/projects.json` v2 validates; downgrade to v1 readable for emergency rollback
- [ ] `migrate_phase2_identity.py --check` reports zero unaudited SQLite writers

## w6-p3: Receipt + Register Envelope, Per-Project Paths
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 2 weeks (week 5–6 of 9-week timeline; ~280 LOC plus ~150 LOC migration helper)
**Dependencies**: [w6-p2]

**Model justification (Sonnet):** Envelope schema is well-specified by plan §5.3 and PRD §7.5. The work is a mechanical addition of `project_id`, `orchestrator_id`, `agent_id` fields to existing JSON envelopes plus a path-resolver change. No cross-cutting design decisions remain after w6-p2. Sonnet is sufficient.

### Description
Move NDJSON write paths under `~/.vnx-data/<project_id>/` for new writes (with backward-compat read fallback to project-local paths). Stamp every event/receipt with the four-tuple as an envelope (additive fields per PRD NFR-9: old readers tolerate missing fields). Update `build_t0_state.py` to read both old (project-local) and new (central) paths, preferring central. Includes a one-shot re-stamper `scripts/migrate_phase3_envelope.py` that walks each project's existing NDJSON and re-streams with the envelope.

### Scope
- Path resolver `scripts/lib/vnx_paths.py` adds `resolve_central_data_dir(project_id)` returning `~/.vnx-data/<project_id>/...`
- Dual-write mode in `append_receipt.py` and `dispatch_register.py` — write to BOTH old per-project path AND new central path; per-project remains source-of-truth until w6-p5
- `build_t0_state.py` reads merge from both sources, preferring central
- Per-project state-rebuild trigger files
- Envelope re-stamper for existing NDJSON

### Files to Create/Modify
- **Create:** `scripts/migrate_phase3_envelope.py` (~150 LOC; offline NDJSON re-stamper)
- **Modify:** `scripts/lib/vnx_paths.py` (+40 LOC; `resolve_central_data_dir`)
- **Modify:** `scripts/append_receipt.py` (+50 LOC; dual-write to old + new path)
- **Modify:** `scripts/lib/dispatch_register.py` (+40 LOC; same)
- **Modify:** `scripts/build_t0_state.py` (+80 LOC; merge-read both sources)
- **Modify:** `scripts/t0_intelligence_aggregator.py` (+30 LOC; accept project_id filter)
- **Modify:** `scripts/lib/state_rebuild_trigger.py` (+20 LOC; per-project trigger files)

### Schema compatibility test (CRITICAL — backward-compat for old readers)
Old readers (pre-envelope, on schema_version 1 receipts) must tolerate new envelope-formatted records. Test:
- Generate two NDJSON files: one with old format `{"event_type":"X","ts":"..."}`, one with new envelope `{"event_type":"X","ts":"...","project_id":"vnx-dev","orchestrator_id":"dev-T0","agent_id":"T1"}`.
- Run the OLD reader (the `build_t0_state.py` from `main` before this PR) against both files. Assert: parses both, treats unknown extra fields as ignorable, returns same `event_type` count.
- Run the NEW reader against both files. Assert: parses both, identity fields default to `None` for old-format lines.

### Success Criteria
- Dual-write produces both `<project>/.vnx-data/state/t0_receipts.ndjson` (legacy) AND `~/.vnx-data/<project_id>/state/t0_receipts.ndjson` (central) on every dispatch
- Both files have identical line counts (modulo race during concurrent writes)
- `build_t0_state.py` produces identical output regardless of which source is preferred
- Re-stamper, run on a copy of historical NDJSON, produces envelope-formatted output that round-trips through the new reader

### Test Plan
- **Unit:**
  - `test_resolve_central_data_dir.py` — path resolution correctness
  - `test_dual_write_atomicity.py` — both files updated; if old-path write fails, new-path write rolls back (and vice versa)
  - `test_envelope_restamper.py` — given old-format input, output has envelope fields populated correctly per source project
- **Integration:**
  - End-to-end dispatch with dual-write enabled — both paths show the new event
  - `build_t0_state.py` against fixture with mixed old + new format — output is correct merge
- **Smoke:**
  - `wc -l` of legacy and central NDJSONs after a soak run agree within 1 line
- **Dry-run mode test:** `migrate_phase3_envelope.py --dry-run --project-id vnx-dev` prints "would re-stamp N lines" without writing anything; assert no file mutation.
- **Schema compatibility test (described above):** old reader tolerates new envelope; new reader tolerates old format. Test against fixture pairs.
- **Cross-project isolation test:** w6-p2 isolation test re-run with envelope active; assert no envelope leakage across project_id boundaries.

### Quality Gate
`gate_w6_p3_envelopes`:
- [ ] Dual-write produces matching line counts on both paths after 100-dispatch soak
- [ ] Old reader (pre-envelope) does not crash on new envelope-formatted NDJSON
- [ ] New reader correctly defaults envelope fields to `None` on old-format lines
- [ ] Re-stamper is idempotent — running it twice on the same input produces identical output
- [ ] `build_t0_state.py` merge-read passes regression suite
- [ ] WAL contention measured before/after — fcntl lock contention does not regress more than 10% (acceptable cost of dual-write)

## w6-p4: One-Shot Data Import (HIGHEST RISK PR IN ROADMAP)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Critical
**Skill**: @backend-developer
**Requires-Model**: opus
**Risk-Class**: critical
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 week (week 7 of 9-week timeline; ~280 LOC main script + ~80 LOC dry-run preflight + ~80 LOC schema migration SQL)
**Dependencies**: [w6-p3]

**Model justification (Opus):** This is THE highest-risk single PR in the entire roadmap. One-shot operation, four source DBs collapsing into one central, ~25 tables touched, FTS5 rebuild required. Under a wrong query, the whole 17.6 GB historical pattern store can be silently mis-attributed. Opus is required for both the design review and the iterative dry-run/snapshot/import/verify cycle.

### Description
A one-shot migration script attaches all 4 source DBs (`vnx-dev`, `mc`, `sales-copilot`, `seocrawler-v2`) and copies their `quality_intelligence.db` and `runtime_coordination.db` rows into `~/.vnx-data/state/quality_intelligence.db` and `~/.vnx-data/state/runtime_coordination.db`, stamping `project_id` per source. Phase 0 columns (already shipped) are extended to the remaining 18 tables via `0011_complete_project_id.sql`. FTS5 indexes are rebuilt with `project_id` in the indexed document. Dual-writer shim keeps per-project DBs in sync until w6-p6 retires them.

### Scope
- Main migrator `scripts/migrate_to_central_vnx.py` — attach + copy + envelope-stamp + transactional commit per project
- Dry-run preflight `scripts/migrate_dry_run.py` — collision detection, schema-drift report, row-count plan, NO writes
- Schema migration SQL `schemas/migrations/0011_complete_project_id.sql` — 18 ALTER TABLE statements for Phase 4 tables
- FTS5 rebuild SQL `schemas/migrations/0012_rebuild_fts5.sql` — drop & recreate `code_snippets_fts` and 4 other FTS5 indexes with project_id in indexed document
- Dual-writer shim `scripts/lib/dual_writer.py` (~30 LOC subset of total) — writes go to BOTH per-project AND central until w6-p6
- Collision-handling rule: prefix colliding keys with `<project_id>:` per plan §5.2
- `pattern_usage.pattern_id` collision fix as bonus side-effect (per audit codex finding §2.12)

### Files to Create/Modify
- **Create:** `scripts/migrate_to_central_vnx.py` (~280 LOC)
- **Create:** `scripts/migrate_dry_run.py` (~80 LOC)
- **Create:** `schemas/migrations/0011_complete_project_id.sql` (~50 LOC)
- **Create:** `schemas/migrations/0012_rebuild_fts5.sql` (~30 LOC)
- **Create:** `scripts/lib/dual_writer.py` (~30 LOC)
- **Modify:** `scripts/append_receipt.py` (+15 LOC; route through dual_writer)
- **Modify:** `scripts/lib/dispatch_register.py` (+15 LOC; same)
- **Create:** `claudedocs/w6-p4-rollback-procedure.md` (operator-readable rollback runbook)

### Risk-Mitigation Steps (CRITICAL — operator-visible)
This PR's success criteria require ALL of the following completed in order:

1. **Schema-drift preflight**: run `scripts/aggregator/schema_drift_report.py --json` from w6-p1; assert each project DB upgraded to v8.2.0-cqs-advisory-oi via `ensure_quality_intelligence_schema.py` BEFORE any read attaches.
2. **Snapshot all 4 source DBs**: `cp -p` each `.vnx-data/state/quality_intelligence.db`, `runtime_coordination.db`, `dispatch_tracker.db` to `~/.vnx-data/snapshots/<ts>/<project_id>/` BEFORE `migrate_to_central_vnx.py` runs. Snapshot retention: 30 days minimum, longer until w6-p6 + 7 days observation passes.
3. **Dry-run first** (mandatory): `migrate_dry_run.py` produces a report listing per-project row counts, collisions detected, FTS5 rebuild plan. Operator visually inspects this report before running the live import. CI gate refuses live import if `dry_run.json` is missing or > 24 hours stale.
4. **Atomic transaction**: live import wraps each project's INSERTs in a single SQLite transaction. Failure mid-project rolls back that project; other projects unaffected.
5. **Idempotent rerun**: every INSERT is `INSERT OR IGNORE` keyed on `(project_id, source_pk)` — re-running the migration is a no-op once successful.
6. **Verification suite**: `migrate_to_central_vnx.py --verify` compares per-project row counts pre/post and computes column-level checksums for sample tables (`success_patterns`, `dispatch_metadata`). Mismatch -> RuntimeError; central DB rolled back.
7. **Dual-writer activation**: only after `--verify` passes. From this point, writes go to both per-project and central; per-project remains source of truth until w6-p5.

### Rollback Procedure (documented in `claudedocs/w6-p4-rollback-procedure.md`)
- **Within 1 hour of import**: `rm ~/.vnx-data/state/quality_intelligence.db`; restart aggregator; per-project DBs are unchanged.
- **Within 7 days, before w6-p5 cutover**: same as above. Dual-writer keeps both DBs in sync until cutover.
- **Post-w6-p5 cutover**: complex — restore central DB from `~/.vnx-data/snapshots/<ts>/`, re-flip `VNX_USE_CENTRAL_DB=0`, restore `.vnx -> .vnx.bak/` symlinks. Worst case: 4 hours of central-DB rebuild work. Per-project DBs are STILL retained (w6-p6 has not run yet).

### Cross-project isolation test (re-run, post-import)
After the import, the cross-project isolation test from w6-p2 is re-run against the CENTRAL DB. Spawn dispatches in 2 fixture projects, each writes a receipt; assert both rows land in central with correct `project_id` and a SELECT filter shows zero leakage.

### Success Criteria
- All 4 projects' rows present in `~/.vnx-data/state/quality_intelligence.db` with correct `project_id`
- Row counts match pre-import per-project totals
- FTS5 indexes rebuild successfully and contain `project_id` as searchable field
- `pattern_usage.pattern_id` collisions resolved via `<project_id>:` prefix (audit codex finding §2.12 fixed as side-effect)
- Dry-run produces a verifiable report; live run matches the dry-run plan
- Snapshot directory `~/.vnx-data/snapshots/<ts>/` exists with 4 sub-dirs (one per project) and is at least the size of the source DBs
- Dual-writer shim active; w6-p3 envelope writes now flow through it
- Verification suite passes for ALL Phase 0 + Phase 4 tables

### Test Plan
- **Unit:**
  - `test_collision_detection.py` — fixture with deliberately colliding `dispatch_id` -> migrator detects and prefixes
  - `test_pattern_usage_prefix.py` — `pattern_id` from project A becomes `vnx-dev:original-id`; from project B becomes `mc:original-id`
  - `test_fts5_rebuild.py` — query for project A's text correctly filters to project A's rows
  - `test_idempotent_rerun.py` — running migrator twice on same input produces identical row counts (INSERT OR IGNORE)
- **Integration:**
  - 4 fixture DBs (smaller versions of real `.vnx-data/state/quality_intelligence.db`) -> migrate -> assert central DB has 4× project_id-stamped rows summing to fixture totals
  - Snapshot integrity: `cp -p` snapshot is byte-identical to source DB at snapshot time
  - Verification suite — pre/post row counts and column checksums match
  - Cross-project isolation test (re-run, post-import)
- **Smoke:**
  - `python3 scripts/migrate_dry_run.py` exits 0 and emits valid JSON report
  - `python3 scripts/migrate_to_central_vnx.py --verify` exits 0
- **Dry-run mode test:** `migrate_dry_run.py` produces a report file at `claudedocs/w6-p4-dryrun-<ts>.md`; assert content matches expected (per-project row counts, collision list, FTS5 rebuild plan). NO file writes outside the report file.
- **Rollback test (CRITICAL — w6-p4-specific):**
  1. Snapshot per-project DBs.
  2. Run live import (in CI fixture environment).
  3. Verify central DB populated.
  4. Execute rollback procedure: `rm ~/.vnx-data/state/quality_intelligence.db`.
  5. Assert per-project DBs are byte-identical to pre-import snapshots.
  6. Assert aggregator still works (Phase 1 deliverable unaffected).
  7. Assert `VNX_USE_CENTRAL_DB=0` reads succeed unchanged.
- **Schema compatibility test:** w6-p3's compatibility test re-run; old readers still tolerate envelope on the central DB.
- **Backward-compat test:** all existing per-project workflows continue working — dispatching on `vnx-roadmap-autopilot` still writes to its per-project DB through dual-writer; receipt processor still ingests; T0 state still rebuilds.

### Quality Gate
`gate_w6_p4_data_import`:
- [ ] Dry-run report generated AND inspected by operator within 24 hours of live import
- [ ] All 4 source DBs snapshotted to `~/.vnx-data/snapshots/<ts>/` with verified byte-identity
- [ ] Schema migration `0011_complete_project_id.sql` applied — `runtime_schema_version` bumps from 10 to 11
- [ ] FTS5 indexes rebuilt; sample query proves `project_id`-filtered results
- [ ] Per-project row count + central row count match exactly post-import
- [ ] Verification suite passes (column-level checksums on `success_patterns`, `dispatch_metadata`, `pattern_usage`)
- [ ] `pattern_usage.pattern_id` synthetic-id explosion (audit §2.12) confirmed mitigated by `<project_id>:` prefix
- [ ] Rollback procedure tested in CI fixture; per-project DB byte-identity preserved post-rollback
- [ ] Dual-writer active — next dispatch writes to BOTH per-project AND central path
- [ ] Cross-project isolation re-test green
- [ ] WAL size on central DB after import is bounded (<1 GB; `wal_autocheckpoint=1000` configured)

## w6-p5: Reader Cutover, Central Install Symlinks
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 week (week 8 of 9-week timeline; ~190 LOC across 8 reader scripts + symlink helper)
**Dependencies**: [w6-p4]

**Model justification (Sonnet):** Reader cutover is mechanical bulk update across ~8 reader scripts. Feature flag `VNX_USE_CENTRAL_DB` provides emergency rollback. The risk is "did we miss a reader" rather than "did we design the cutover wrong"; missed readers are caught by the bulk-audit grep. Sonnet handles the mechanical rewrite well.

### Description
Each of the 4 projects gets `.vnx -> ~/.vnx-system` symlinked. Project-local `.vnx/` directories become `.vnx.bak/`. Eight reader scripts (`gather_intelligence.py`, `dashboard/api_intelligence.py`, `query_quality_intelligence.py`, `intelligence_selector.py`, `cached_intelligence.py`, etc.) switch to reading central DB by default. Feature flag `VNX_USE_CENTRAL_DB=0` reverts to per-project for emergencies. `subprocess_dispatch.py` spawns workers with `VNX_DATA_DIR=~/.vnx-data` (central) and `VNX_PROJECT_ID=<resolved>`. Worker reads central paths.

### Scope
- 8 reader scripts default-DB path becomes central
- Feature flag honor: `VNX_USE_CENTRAL_DB=0` reverts to per-project
- Symlink helper script `scripts/migrate_phase5_cutover.sh` — symlinks each project to central install
- Bulk-audit script `scripts/audit_central_readers.sh` — greps SQL for `success_patterns`, `pattern_usage` etc. without `project_id` in WHERE; fails if found
- `vnx-sql <project_id>` operator wrapper opens DB with `PRAGMA query_only=1` plus a `WHERE project_id=?` lens (per plan §7.2 mitigation)

### Files to Create/Modify
- **Modify:** `scripts/gather_intelligence.py` (+30 LOC; default DB path central)
- **Modify:** `scripts/lib/intelligence_selector.py` (+25 LOC; same)
- **Modify:** `scripts/lib/cached_intelligence.py` (+20 LOC; same)
- **Modify:** `scripts/query_quality_intelligence.py` (+25 LOC; same)
- **Modify:** `dashboard/api_intelligence.py` (+30 LOC; same)
- **Modify:** `scripts/lib/subprocess_dispatch.py` (+15 LOC; central env vars in spawn)
- **Modify:** `scripts/lib/vnx_paths.py` (+25 LOC; `prefer_central` flag from env)
- **Create:** `scripts/migrate_phase5_cutover.sh` (~20 LOC; symlink switcher per project)
- **Create:** `scripts/audit_central_readers.sh` (~10 LOC; CI-runnable grep audit)
- **Create:** `scripts/lib/vnx_sql.py` (~10 LOC; operator wrapper)

### Success Criteria
- All 8 reader scripts default to central DB; `VNX_USE_CENTRAL_DB=0` reverts to per-project
- Each project symlinked: `<project>/.vnx -> ~/.vnx-system`
- `<project>/.vnx.bak/` retains the prior install for emergency rollback
- Bulk-audit grep finds zero un-namespaced reader queries on namespaced tables
- Cross-project pattern learning works: a pattern learned in `mc` is consultable from `vnx-dev` (filterable via `project_id` but, by default, the orchestrator can opt to share)

### Test Plan
- **Unit:**
  - `test_central_reader_default.py` — given `VNX_USE_CENTRAL_DB` unset or `=1`, reader hits central DB
  - `test_central_reader_flag_off.py` — `VNX_USE_CENTRAL_DB=0` reverts to per-project DB; existing tests pass unchanged
  - `test_audit_grep.py` — fixture with un-namespaced SELECT on `success_patterns` -> audit script fails
- **Integration:**
  - End-to-end dispatch on `vnx-roadmap-autopilot` with cutover active — worker reads central DB, writes to both (dual-writer still on); receipt arrives correctly
  - Cross-project query: `query_quality_intelligence.py --project-id mc` returns mc's rows from central DB
- **Smoke:**
  - `bash scripts/audit_central_readers.sh` exits 0 (no un-namespaced readers)
  - `vnx-sql vnx-dev "SELECT COUNT(*) FROM success_patterns"` returns the expected count
- **Dry-run mode test:** `migrate_phase5_cutover.sh --dry-run --project /path/to/project` prints "would symlink: ... -> ..." without making any filesystem change.
- **Backward-compat test (CRITICAL):** `VNX_USE_CENTRAL_DB=0` is the rollback path. With the flag off, every existing test in `tests/runtime/`, `tests/governance/`, `tests/dispatch/`, `tests/intelligence/` passes unchanged.
- **Cross-project isolation test (final form):** with central DB live, simulate concurrent dispatches in 2 projects; verify zero pollution. Verify `terminal_leases` keyed on `(project_id, terminal_id)` per plan §7.5.

### Quality Gate
`gate_w6_p5_reader_cutover`:
- [ ] All 8 reader scripts updated; CI grep audit green
- [ ] `VNX_USE_CENTRAL_DB=0` rollback path validated in CI
- [ ] Symlink switcher idempotent — re-running on already-cutover project is no-op
- [ ] Cross-project pattern learning operational (small E2E demo: dispatch on `vnx-dev` produces a receipt; later dispatch on `mc` can consult the pattern)
- [ ] `terminal_leases` collision (plan §7.5) mitigated — multi-project simultaneous dispatch test green
- [ ] WAL size on central DB stable; `wal_autocheckpoint=1000` honored
- [ ] Dual-writer still active (only retired in w6-p6); per-project DB still receiving writes

## w6-p6: Per-Project DB Retirement
**Track**: A
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1–2 days (week 9; ~80 LOC; lots of deletion + 30-day cron)
**Dependencies**: [w6-p5] AND [≥7 days stable Phase 5 operation]

**Model justification (Sonnet):** Cleanup PR. By this point Phase 5 has run for 7+ days. Mechanical deletion of dual-writer + 30-day retention cron. Sonnet sufficient.

### Description
After ≥7 days of Phase 5 stable operation, rename per-project DBs to `quality_intelligence.db.pre-central-2026-XX-XX` and stop writing to them. Schedule 30-day retention sweep via launchd. Delete `dual_writer.py` shim (~50 LOC removed) and the legacy-path branches in `append_receipt.py` and `dispatch_register.py`. Operator postmortem template added.

### Scope
- Per-project DB rename to `.pre-central-2026-XX-XX`
- 30-day retention sweep (launchd plist `com.vnx.retention-sweep.plist`)
- Remove dual-writer shim and legacy-path branches
- Codex gate at end-of-feature: total review of P1–P6 cohesion
- Postmortem markdown stub

### Codex gate placement
Per the gate-placement strategy: this PR carries the **end-of-feature codex_gate** for the entire Phase 6 (P1-P6) migration. Codex reviews not just w6-p6's own changes but the cumulative migration — schema correctness, identity propagation across all PRs, dual-writer retirement safety, snapshot retention, FTS5 index health, WAL discipline.

### Files to Create/Modify
- **Create:** `scripts/cleanup_per_project_dbs.sh` (~25 LOC; rename script with safety check that ≥7 days passed)
- **Delete:** `scripts/lib/dual_writer.py` (~50 LOC removed)
- **Modify:** `scripts/append_receipt.py` (-30 LOC; legacy-path writer removed)
- **Modify:** `scripts/lib/dispatch_register.py` (-25 LOC; same)
- **Create:** `~/Library/LaunchAgents/com.vnx.retention-sweep.plist` (~20 LOC; 30-day sweep)
- **Create:** `claudedocs/2026-XX-XX-central-vnx-cutover-postmortem.md` (operator postmortem template — content filled by operator after run)

### Success Criteria
- Per-project DBs renamed; central DB is sole authoritative source
- Dual-writer shim and legacy-path branches removed
- 30-day retention sweep installed and scheduled
- Postmortem stub committed; awaits operator content
- Operator can no longer accidentally write to per-project DB (rename means path no longer resolves to a writable file)

### Test Plan
- **Unit:**
  - `test_cleanup_safety_check.py` — script refuses to run if `<7 days since w6-p5 merged`
  - `test_retention_sweep.py` — sweep deletes files older than 30 days; younger files preserved
- **Integration:**
  - End-to-end dispatch post-cleanup — receipt lands in central path only; legacy path no longer written
  - Aggregator still works (it was always read-only and now reads central)
- **Smoke:**
  - `ls <project>/.vnx-data/state/*.pre-central-*.db` returns the renamed file
  - `launchctl list | grep com.vnx.retention-sweep` shows the agent loaded
- **Dry-run mode test:** `cleanup_per_project_dbs.sh --dry-run` prints "would rename: ... -> ..." without making any filesystem change.
- **Rollback test:** if needed within 30 days, un-rename the DBs and restore dual-writer; verify dispatch goes back through both paths.
- **Backward-compat test:** all existing tests (now central-only) green.

### Quality Gate
`gate_w6_p6_db_retirement`:
- [ ] Per-project DBs renamed with `.pre-central-<ts>` suffix
- [ ] Dual-writer shim deleted; `append_receipt.py` and `dispatch_register.py` legacy branches removed
- [ ] 30-day retention sweep installed
- [ ] Postmortem template committed
- [ ] **Feature-end codex_gate**: codex reviews entire Phase 6 (P1-P6) migration; assesses schema correctness, identity propagation, dual-writer retirement safety, snapshot retention, FTS5 index health, WAL discipline. Codex blocking findings = PR remains open.
- [ ] Total LOC delta across Phase 6: ~1,470 added + ~105 removed = ~1,365 net (within plan's 1,950–2,400 LOC envelope; lower because Phase 0 already shipped)
- [ ] Cross-project pattern learning verified live (the headline operator deliverable of this entire 9-week migration)

---

## Feature-End Quality Gate (Phase 6 cohesion)

`gate_phase06_migration_complete`:
- [ ] All 6 sub-PRs merged in dependency order
- [ ] Cross-project pattern learning live (mc patterns inform vnx-dev dispatches and vice versa)
- [ ] No regressions in any per-project workflow
- [ ] All snapshot retention windows honored (30 days for `.pre-central-*.db`; 30 days for `~/.vnx-data/snapshots/<ts>/`)
- [ ] WAL size bounded; checkpoint discipline working
- [ ] No orphaned `_unknown` project_id rows in central DB
- [ ] Aggregator dashboard (w6-p1 deliverable) shows all 4 projects unified
- [ ] Codex final-pass gate green (run on w6-p6 PR)
