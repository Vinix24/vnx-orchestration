# ADR-009 — Schema-First Migrations via PRAGMA Introspection (No Hardcoded Column Projections)

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** P4 nine-round fix-forward chain root cause; future migration discipline

## Context

The P4 data migration (PR #432, `scripts/migrate_to_central_vnx.py` ~1757 LOC) consolidated four per-project SQLite stores into the central VNX state DBs. It took **nine codex review rounds** and three `--apply` attempts to converge. The full reflection lives in `claudedocs/2026-05-09-p4-migration-architecture-lessons.md`.

A pattern emerged across rounds 7, 8, and 9: each round surfaced a different instance of the **same bug class** — a hardcoded column list, table tuple, or index projection that should have been derived from schema introspection.

- **Round 7:** `dispatch_experiments` had a hardcoded column list that drifted from the deployed schema in two of four source DBs.
- **Round 8:** `quality_system_metrics` and `scan_history` had hardcoded INSERT projections that omitted columns added in a later migration.
- **Round 9:** `code_snippets` had a hardcoded 12-column projection that fell out of sync with the FTS5 rebuild.

In each case the bug was: code that was *flexible enough* to handle the canonical schema but failed silently when a deployed DB had **extra** columns from local schema drift. The fix-forward pattern was: detect the omission, hardcode the new columns, ship — until the next round found a different table with the same shape of bug.

The P4 lessons doc Sections 4.1 and 4.2 codify the structural fix:

> "Schema-first beats imperative-first for multi-tenant migrations. Define a target central schema explicitly, not derived from per-source ALTERs. The importer becomes a generic engine driven by descriptors. Adding a new source = adding a descriptor, not editing the engine."

Round-6 introduced `_rebuild_one_table_dynamic` — a helper that derives the column list from `PRAGMA table_info` at apply time and reconstructs the rebuilt table from that introspection rather than from a hardcoded SQL string. Where this helper was used, no rounds-7-through-9 bugs were found. Where it was *not* used, each round found another instance.

This ADR makes schema-first the binding rule for all future VNX migrations.

## Decision

**All future VNX migrations MUST derive column lists, table shapes, and index definitions from `PRAGMA table_info` and `sqlite_master.sql` introspection at apply time. Hardcoded SQL column tuples and hardcoded table projections are prohibited in migration code.**

Concretely:

1. **Column projection rule.** Code that copies rows from a source table to a rebuilt target table MUST list columns by querying `PRAGMA table_info(<table>)` against the actual deployed schema, not by typing `(col1, col2, ..., colN)` literals. The canonical helper is `_rebuild_one_table_dynamic` (in `scripts/migrate_to_central_vnx.py` after PR #432 round-6, at approximately lines 720-820).

2. **Index reconstruction rule.** When a UNIQUE rebuild requires recreating indexes, the migration code MUST read existing index DDL from `sqlite_master.sql WHERE type='index' AND tbl_name=?` and replay it after the rebuild. Indexes are not enumerated in code.

3. **ALTER ↔ IMPORT_TABLES symmetry rule.** Every `ALTER TABLE … ADD COLUMN` in `schemas/migrations/*.sql` must have a matching entry in the importer's table-import descriptor (or be picked up automatically by introspection). The structural test in `tests/test_migrate_dry_run.py` parses migrations and the importer's `IMPORT_TABLES` constant and fails CI if they disagree.

4. **Composite UNIQUE rebuilds use dynamic schema reconstruction.** When a migration converts a single-column UNIQUE to a composite UNIQUE (per ADR-007), it MUST use `_rebuild_one_table_dynamic` rather than a hand-written `CREATE TABLE … new_col_list … INSERT INTO new SELECT col_list FROM old` SQL block. The hand-written form is the bug pattern caught in rounds 7-9.

5. **Schema-drift detector as preflight.** Before `--apply`, the migration tool runs a preflight that scans all source DBs and emits a column-by-column comparison table. Drift cases (autopilot has `project_id`, others don't) are surfaced explicitly to the operator rather than discovered mid-import (lessons doc Section 7 action item #14).

6. **Helpers, not policies.** The schema-first discipline is enforced through library helpers (`_rebuild_one_table_dynamic`, `_collect_columns`, `_collect_indexes`) that make the right thing easy. Migration authors who write hardcoded SQL are caught at codex_gate review.

## Reasoning

1. **The bug class is real, recurring, and structurally fixable.** Nine rounds of P4 surfaced multiple instances of the same root cause. Each fix was correct in isolation but did not raise the floor — the next round found the next instance. Schema-first eliminates the *bug class*, not individual bugs. Lessons doc Section 1.2: "The tests grew with the bugs, not ahead of them. Each round added a regression test for the specific bug being fixed but did not raise the floor of coverage."

2. **Hardcoded projections embed environment assumptions.** A hardcoded column list assumes the deployed schema matches the canonical schema. In production, deployed DBs drift: a column added by an out-of-band migration, a column added by a partial-rollback, a column added by a feature flag. Introspection reads what is actually there. Lessons doc Section 4.1: the imperative-first design "makes runtime decisions that is flexible but also the source of T4 (verifier co-blindness) and partly of T1."

3. **Round-6's `_rebuild_one_table_dynamic` is empirical evidence the pattern works.** Tables that used the dynamic helper after round 6 saw zero round-7/8/9 findings. Tables that retained hardcoded projections saw one finding each per round. The correlation is causal: introspection eliminates the missing-column class.

4. **Schema is data, not code.** Treating the deployed schema as source-of-truth (via introspection) and the migration as a generic transformation engine separates "what to do" from "what the data looks like." This is the standard data-engineering pattern (Singer taps, dbt, Liquibase) and it is well-understood. VNX deviated for historical reasons (the original migrations were one-off scripts) and paid the cost in rounds 7-9.

5. **It composes with ADR-007.** Composite UNIQUE rebuilds (per ADR-007) require recreating the table with a new constraint. Without introspection, the rebuild script must know every column. With introspection, the rebuild adapts to whatever the deployed table actually has. ADR-007 and ADR-009 are paired: the multi-tenant constraint pattern only stays maintainable because the migration discipline is schema-first.

6. **Tests catch ALTER ↔ IMPORT_TABLES drift, not engineers.** Lessons doc Section 3.2: the missing test was "size-realistic FTS rebuild." The missing test for round-7-9 bugs is "ALTER ↔ IMPORT_TABLES symmetry." That test is mechanical to write (parse the SQL on both sides, set-difference, fail if non-empty) and catches the bug class at CI time rather than at round-N codex review.

7. **Open items OI-1375 and OI-1376 are the carry-forward work.** Some round-9 changes still need a schema-first rewrite for migration 0016. ADR-009 makes the rewrite mandatory rather than optional, and the structural test ensures future migrations don't reintroduce the pattern.

8. **The discipline is library-enforceable, not policy-enforceable.** Asking engineers to "remember to introspect" is the same shape as asking engineers to "remember to filter by `project_id`." It does not scale. Library helpers (`_rebuild_one_table_dynamic`) plus codex_gate's review of hardcoded SQL projections enforce the rule structurally. Migration authors who try to hand-roll a column list are caught at gate time.

## Consequences

### Accepted

- New migrations get the schema-introspection-by-default helper. `_rebuild_one_table_dynamic` and its sibling helpers are the canonical entry points for any rebuild.
- The structural test `tests/test_migrate_dry_run.py` parses `schemas/migrations/*.sql` and the importer's `IMPORT_TABLES` constant and enforces ALTER↔IMPORT symmetry at CI time.
- Codex_gate review contracts include a finding category "hardcoded column projection in migration code" with severity blocking. New PRs that hand-roll column lists are caught at review.
- The schema-drift preflight (lessons doc Section 7 #14) becomes part of `--dry-run` output. Operators see drift before `--apply` rather than mid-import.
- ADR-007 (project_id stamping) composability: composite UNIQUE rebuilds use `_rebuild_one_table_dynamic` with the new constraint specified declaratively, not by hand-writing the rebuild SQL.
- Migration 0016 follow-up rewrites (per OI-1375 + OI-1376) are mandatory and tracked under this ADR.

### Rejected

- **Imperative-first migration scripts.** Hand-rolled `CREATE TABLE new (...) AS SELECT col_a, col_b FROM old` is rejected. Use `_rebuild_one_table_dynamic`.
- **Hardcoded column tuples in migration code.** Variables like `IMPORT_COLS = ('id', 'created_at', ..., 'project_id')` are rejected. Derive from `PRAGMA table_info`.
- **"Just list the columns explicitly for clarity."** Explicit is not always clearer when it embeds environment assumptions. Introspection is more explicit because it adapts to reality. The audit trail (logs of introspected columns) is the explicit evidence, not the source code.
- **One-off migration scripts that bypass the helper library.** Migrations that "just need to do this one thing" are exactly the migrations that drift from the central pattern. All migrations route through the canonical helpers.
- **Skipping the structural test "for this PR only."** ALTER↔IMPORT symmetry is the smallest possible test of this ADR. It runs in milliseconds; there is no excuse.

## Cross-references

- ADR-007 — Multi-tenant `project_id` stamping pattern (the constraint pattern that depends on schema-first migrations to stay maintainable)
- `claudedocs/2026-05-09-p4-migration-architecture-lessons.md` §1.2, §1.3, §2 (T1+T3+T6 bug patterns), §4.1 (schema-first beats imperative-first), §4.2 (composite uniqueness as default), §7 (action items)
- PR #432 (round-6 `_rebuild_one_table_dynamic` introduction; rounds 7/8/9 fix-forward)
- Open items OI-1375 + OI-1376 (post-432 rewrite required for migration 0016)
- `scripts/migrate_to_central_vnx.py` (canonical helpers `_rebuild_one_table_dynamic`, `_collect_columns`, `_collect_indexes`)
- `schemas/migrations/0010_add_project_id.sql`, `0015_complete_project_id.sql`, `0016_rebuild_fts5.sql`
- `tests/test_migrate_dry_run.py` (structural test enforcing ALTER↔IMPORT symmetry)
