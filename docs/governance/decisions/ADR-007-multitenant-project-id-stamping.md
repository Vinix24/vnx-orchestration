# ADR-007 — Multi-tenant `project_id` Stamping Pattern

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Phase 0 + P4 multi-tenant migration design (PRs #410, #412, #416, #431, #432)

## Context

VNX began as a single-tenant orchestration system: one operator, one project (`vnx-roadmap-autopilot`), one local SQLite stack under `.vnx-data/state/`. By Q1 2026 the operator was running **four** distinct VNX deployments (`vnx-dev`, `mc`, `sales-copilot`, `seocrawler-v2`) totaling ~17.6 GB of `.vnx-data/`, each with its own per-project `quality_intelligence.db` and `runtime_coordination.db`. Patterns learned in `mission-control` could not inform a dispatch in `sales-copilot`. Cross-project learning was structurally impossible.

`claudedocs/2026-04-30-single-vnx-migration-plan.md` proposed Model B: separate repos, central VNX install, **central data dir namespaced by `project_id`**. Phase 0 (PR #410) added `project_id` columns to seven hot tables; Phase 4 (PR #432) imported all four projects' data into the central DBs and stamped every row with its source `project_id`.

The migration ran for **nine codex review rounds**. Each round surfaced new instances of the same root pattern: tables, indexes, or constraints that assumed single-tenancy. The lessons doc at `claudedocs/2026-05-09-p4-migration-architecture-lessons.md` Section 1.3 documents how `INSERT OR IGNORE` against a single-column UNIQUE constraint silently dropped re-stamped rows because the existing `DEFAULT 'vnx-dev'` rows already occupied the PK slot. Section 4.2 codifies the fix: composite uniqueness as the default.

This ADR captures the resulting pattern as a binding architectural rule for all future central-DB tables.

## Decision

**All multi-tenant tables in central VNX state DBs (`quality_intelligence.db`, `runtime_coordination.db`, `dispatch_tracker.db`) carry a `project_id TEXT NOT NULL DEFAULT 'vnx-dev'` column, and every UNIQUE constraint or PK on a natural key is composite over `project_id`.**

The pattern has three concrete rules:

1. **Column rule.** Every table that holds tenant-scoped state declares `project_id TEXT NOT NULL DEFAULT 'vnx-dev'`. The default exists only as a forward-compatible bridge for legacy single-tenant rows; the importer overwrites it with the source's actual project id at migration time. New code SHOULD explicitly stamp `project_id` and SHOULD NOT rely on the default.

2. **Constraint rule.** Where a table previously had `UNIQUE(natural_key)`, the central form is `UNIQUE(natural_key, project_id)` (or the inverse — order does not matter for SQLite UNIQUE semantics, but the convention places `project_id` last). Single-column UNIQUE constraints on tenant-scoped natural keys are a smell that requires per-table justification recorded in the migration file.

3. **Identifier-rewrite rule.** Where two source projects can mint the same identifier (e.g., `dispatches.id=1`, `terminal_leases.id=1`), the importer prefix-rewrites with `<project_id>:<source_id>` for shared identifier columns. This is the same rule applied successfully to `pattern_usage.pattern_id` (which fixed the pre-existing single-project synthetic-id explosion as a bonus side effect, per `claudedocs/2026-04-30-single-vnx-migration-plan.md` §5.2).

The structural test in `tests/test_migrate_dry_run.py` parses migration `0015_complete_project_id.sql` at CI time and asserts every multi-tenant table conforms. New migrations that introduce tenant-scoped tables MUST extend this test.

## Reasoning

1. **The whole value proposition rests on this.** The single-VNX move (`claudedocs/2026-04-30-single-vnx-migration-plan.md` §1.2 "Why B over A") chose Model B specifically because column-namespacing gives strong-via-column isolation while keeping reversible per-project rollback paths. Without `project_id` stamping there is no Model B — there is only Model A (monorepo) or Model C (read-only federation). The operator rejected both. `project_id` is therefore not a tactical column, it is the identity layer of the entire central system.

2. **It also fixes a latent single-project bug.** The `pattern_usage.pattern_id` synthetic-id explosion (codex finding §2.12, referenced in migration plan §5.2) was a single-project bug masked by single-project assumptions. Composite-key namespacing fixes it as a free side effect — every centralized table inherits stable per-project identity.

3. **Multi-tenant retrofits are the highest-risk class of change in this codebase.** Lessons doc Section 2 taxonomy: bug patterns T1, T3, T6 (and partially T9) are migration-specific; bug patterns T2, T4, T7, T8 are general data-engineering. The migration-specific cluster — DEFAULT-as-sentinel, single-column PK in multi-tenant world, dedup wider than tenancy — exists precisely because retrofit code carries forward single-tenant assumptions invisibly. Codifying composite UNIQUE as the default is the structural antidote.

4. **Codex round-9 surfaced that not all tables conform yet.** Open items OI-1375 and OI-1376 enumerate the residual gaps: tables added between rounds 5 and 8 that did not pick up the new pattern. This ADR makes the pattern binding so future additions are caught at design time, not by round-N audit.

5. **Verifier independence depends on it.** Lessons doc Section 4.5: the verifier (`scripts/verify_central_vnx.py` after refactor) reads central DBs through an independent connection and computes per-`(project_id, table, key)` checksums. The verifier's contract presumes `project_id` is canonical; if it is missing, the verifier silently degrades into the importer's blind spot (bug pattern T4). Mandating stamping at design time keeps verifier and importer structurally separable.

6. **The composite UNIQUE pattern is itself a contract with the importer.** `INSERT OR IGNORE` is safe only when the UNIQUE constraint already encodes the intended dedup semantics (lessons doc T2). Without `project_id` in the UNIQUE, IGNORE collapses cross-tenant rows. With it, IGNORE preserves per-tenant uniqueness while still allowing replay-safe imports. The constraint and the conflict policy are not independent design decisions — they are paired.

7. **MCP / cross-project learning depends on it.** The strategic replan (`claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M4) calls out: "MCP has no canonical multi-tenant pattern for SQLite. RLS is Postgres-only. We are state of the art." Wave 4 of that plan exposes central DBs as MCP servers; the MCP server must filter on `project_id` from the session header. If the column does not exist or is not authoritative, MCP cannot safely federate across the operator's projects.

## Consequences

### Accepted

- Every new central-DB table MUST be `project_id`-stamped at design time. Migrations that add tenant-scoped tables without `project_id` are rejected at codex_gate.
- Every UNIQUE constraint on a natural key in a multi-tenant table MUST include `project_id` as a composite component. Single-column UNIQUE in central is a smell that requires per-table justification documented in the migration file.
- The importer (`scripts/migrate_to_central_vnx.py`) and the verifier (`scripts/verify_central_vnx.py`) treat `project_id` as canonical evidence. Verifier per-project COUNT(*) discrepancies are blocking findings.
- FTS5 rebuilds (e.g., migration `0016_rebuild_fts5.sql`) must include `project_id` in the indexed document so cross-tenant FTS queries can be project-scoped at query time.
- The structural test `tests/test_migrate_dry_run.py` parses `schemas/migrations/0015_complete_project_id.sql` and enforces conformance.
- Open items OI-1375 and OI-1376 track residual non-conforming tables; they remain blocking until closed.

### Rejected

- **Single-tenant table additions in the central DBs.** Any new feature that adds a table without `project_id` is rejected at codex_gate.
- **Post-hoc `project_id` retrofitting at query time.** Filtering by joining to a `project_assignments` side-table or reconstructing tenancy from `dispatch_id` prefixes is rejected — these were the patterns that produced the round-3 to round-9 fix-forward chain.
- **`DEFAULT 'vnx-dev'` as a sentinel for legitimate rows.** The default exists only as a column-add bridge during migration. New writers MUST stamp `project_id` explicitly. Lessons doc Section 4 action item #8 reinforces: future column-add migrations should prefer `DEFAULT NULL` + explicit post-migration UPDATE + `CHECK` constraint over `DEFAULT 'vnx-dev'`.
- **Cross-tenant deduplication.** Rolling up `tag_combinations`, `success_patterns`, etc. across projects without `project_id` partition is the bug pattern T6. Aggregates that intentionally cross tenants must do so explicitly via a derived view, not by omitting `project_id` from the underlying table.

## Implementation note — 2026-06-14 (1.0.1 future-state reconciliation)

The `dispatches` table (`runtime_coordination.db`) is now brought into this ADR's
composite-key pattern, and the supporting version-reconciliation machinery is
implemented. This is the database-engineer-led future-state batch (PRD
`claudedocs/PRD-future-state-reconciliation-v1.1.md`), distinct from the still-
planned broader SPC/intelligence-table batch (#864).

- **PR-A1 (#859) — in-place `dispatches` rebuild.** `scripts/migrate_future_system.py`
  performs a schema-preserving, crash-safe, in-place rebuild that swaps any
  uniqueness keyed solely on `dispatch_id` for a composite
  `UNIQUE(dispatch_id, project_id)`. Per R1.1 it removes the single-key
  constraint in **every** form — inline column `UNIQUE`, table-level
  `UNIQUE(dispatch_id …)`, standalone `CREATE UNIQUE INDEX`, partial unique
  indexes, and `lower(dispatch_id)` expression indexes — detected by parsing
  `sqlite_master` SQL, not PRAGMA alone. The rebuild follows the canonical
  12-step order (capture/restore `PRAGMA foreign_keys`; `BEGIN IMMEDIATE` with
  bounded retry; recreate dependent views/triggers verbatim; preserve the
  `sqlite_sequence` high-water mark; `foreign_key_check` + `integrity_check`
  before commit), preserving FKs, CHECKs, collations, generated columns, and
  non-target indexes. Tenant `project_id` is resolved **fail-closed** from a
  precedence chain (resolved DB path → `.vnx-project-id` marker →
  `VNX_PROJECT_ID`): conflicting or unknown sources abort, and existing
  NULL/empty/conflicting `project_id` values abort before any mutation. This
  upholds the Decision's constraint rule and the rejected "`DEFAULT 'vnx-dev'`
  as a sentinel" consequence — there is **never a silent `vnx-dev` default** at
  the live runtime tier.

- **PR-A2 (#861) — version reconciliation via a declarative invariant manifest.**
  `scripts/lib/schema_manifest.py` replaces name-based version checks with a
  per-version (v22–v30) invariant manifest (tables; columns with type +
  nullability; PK ordinals; FK actions; index definitions; views). A DB whose
  claimed `user_version` fails its invariant is downgraded to the highest version
  whose invariants actually hold and the missing migrations re-run; on no safe
  target it raises rather than guess (the ADR-009 schema-first discipline this
  ADR depends on for enforceability).

- **PR-B (#863) — tenant-scoped reads.** `scripts/build_t0_state.py` reads
  canonical tracks and `track_open_items` only with a `WHERE project_id = ?`
  predicate, degrading to an explicit `tenant_unavailable` flag (empty rows)
  rather than ever returning cross-tenant rows.

The bridge that maintains `track_open_items` (PR-C #862) likewise scopes all
access by `(track_id, project_id)`. The operator runtime-migration runbook for
applying the in-place rebuild against a live DB (quiesce, verified backup,
preflight + dry-run, postflight `integrity_check`, restore-on-failure) is in
`docs/MIGRATION_GUIDE.md` (PRD §7.2, human-gated).

## Cross-references

- ADR-009 — Schema-first migrations via PRAGMA introspection (the implementation discipline that keeps this ADR enforceable across future migrations)
- `claudedocs/2026-04-30-single-vnx-migration-plan.md` §3 (identity layer), §4 (schema migration), §5.2 (ID collisions)
- `claudedocs/2026-05-09-p4-migration-architecture-lessons.md` §1.3, §2 (bug taxonomy T1/T3/T6), §4.2 (composite uniqueness as default)
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M4 (multi-tenant project_id stamping)
- PR #410 (Phase 0 column add), PR #412/#416 (verifier), PR #431/#432 (P4 data import), open items OI-1375 + OI-1376
- `schemas/migrations/0010_add_project_id.sql`, `schemas/migrations/0015_complete_project_id.sql`, `schemas/migrations/0016_rebuild_fts5.sql`
- `scripts/migrate_to_central_vnx.py`, `scripts/verify_central_vnx.py`, `tests/test_migrate_dry_run.py`
- 1.0.1 future-state batch: `scripts/migrate_future_system.py` (PR-A1 #859), `scripts/lib/schema_manifest.py` (PR-A2 #861), `scripts/build_t0_state.py` (PR-B #863), `scripts/import_open_items_to_tracks.py` + `scripts/lib/tracks.py` (PR-C #862), `scripts/roadmap_manager.py` (PR-D #871), and the PRD `claudedocs/PRD-future-state-reconciliation-v1.1.md`
- Operator runbook: `docs/MIGRATION_GUIDE.md` §6 (runtime migration, human-gated)
