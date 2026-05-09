# Database/Migration Bug Taxonomy

Distilled from the P4 migration fix-forward chain (5 rounds, 14+ bugs). Each pattern includes a VNX example, a generalised lesson, and a "what to look for" rule.

| ID | Pattern | VNX example | Generalised lesson |
|---|---|---|---|
| **T1** | DEFAULT-as-fallback that doubles as a sentinel | `0015_complete_project_id.sql:27-61` adds `DEFAULT 'vnx-dev'`. The importer assumed every row would be re-stamped; rows that weren't stamped masqueraded as legitimate `vnx-dev` rows | Use a sentinel that is INVALID (e.g. `__UNSET__`) + a NOT NULL CHECK that explicitly rejects it post-migration. Make "row exists with no project context" an error, not a default |
| **T2** | INSERT OR IGNORE skip-on-conflict | `migrate_to_central_vnx.py:1052` silently kept the older `vnx-dev` row when the autopilot import tried to write the same `(id)` PK. The re-stamp at line 1008-1009 never happened because the INSERT was discarded entirely | Default to UPSERT (`ON CONFLICT … DO UPDATE`) for migrations. Use IGNORE only when the unique constraint already encodes the intended dedup semantics (i.e. the constraint includes `project_id`) |
| **T3** | Single-column PK in a multi-tenant world | `terminal_leases.id`, `execution_targets.id`. Different projects produced overlapping `id=1..3` values. Whichever project wrote first won; others silently dropped | Composite `(project_id, source_rowid)` UNIQUE in central, OR rewrite IDs at the import boundary. Already done for `p4_import_idempotency` (line 873) — should be the default for every imported table that has a numeric PK |
| **T4** | Verifier shares blind spots with importer | `_compare_counts` and `_import_table` both call `_column_exists` to decide whether to scope by `project_id`. If the helper has a bug, both inherit it. The R5 `code_snippets` verify mismatch was structurally invisible | Verifier must use INDEPENDENT evidence: separate connection, separate code path, ideally separate process. Read source via different connection and recompute checksums on canonical fields |
| **T5** | Bootstrap path assumes pre-existing schema | R3: `--apply v1` produced empty central. `_init_central_if_missing` wasn't being called when central DB file existed but was empty/stale | Bootstrap and migration-apply should be separate, gated commands. Avoid implicit "is this greenfield?" detection from filesystem state — make the operator declare `--fresh-central` |
| **T6** | Dedup wider than tenancy | `tag_combinations.tag_tuple` UNIQUE. Source had 5527 rows for mc, central got 1051 — different projects' overlapping tags collided on the cross-tenant unique key | Every table with a "natural" unique key needs `project_id` added to that uniqueness when imported, OR the importer must namespace the natural key (the `_prefix_value` pattern, applied table-by-table) |
| **T7** | Performance via correlated subquery on unindexed column | `0016_rebuild_fts5.sql:60-65` SELECT in INSERT, `snippet_metadata.snippet_rowid` had no index. 727k snippets × 119k metadata = ~87B row scans, ~3-5h ETA | Migration files MUST include `CREATE INDEX` statements as part of the migration for any join column they correlate against. Add a test fixture sized to hit the bad path (>100k rows) |
| **T8** | Read-failure absorbed as zero | R2: missing/unreadable source table degraded silently to count=0. The importer thought it had imported zero rows from a healthy source | "Absent" and "unreadable" must be distinguishable codes. Done via `read_errors` list with structured types: `central_table_missing`, `source_table_unreadable`, `source_attach_failed` |
| **T9** | run_id not scoped → cross-run skip-bleed | `p4_import_skipped` was global; an old skip from run 1 contaminated run 2's verify. `verify_import` saw the stale skip and reported a false-positive discrepancy | Every retryable persistent record needs a `run_id` AND a `resolved_at` so cross-run state can be filtered correctly |
| **T10** | WAL snapshot inconsistency | R2: source attached read-only via WAL could see partial writes mid-source-process if the source had a live writer. The pre-snapshot tar.gz captured an inconsistent point-in-time | Snapshot via `BEGIN IMMEDIATE` on the source, OR `Connection.backup(dest)`, OR `wal_checkpoint(TRUNCATE) + cp`. Never bare `shutil.copy2` of a WAL DB |

## Categorising the patterns

**Migration-specific** (only matter when retrofitting single-tenant data into multi-tenant central): T1, T3, T5, T6, partly T9.

**General data-engineering** (apply to any SQLite work): T2, T4, T7, T8, T10.

The migration-specific cluster suggests **multi-tenant retrofits** of single-tenant schemas are the highest-risk class of change. Any new "consolidate per-X DBs into central" work should treat T1, T3, T6 as default failure modes to design against, not bugs to discover.

## Diagnosis quick-rules

Operator says "rows are missing" or "wrong count":
1. Run `EXPLAIN QUERY PLAN` on the importer's INSERT — look for `SCAN TABLE` on inner-loop tables (T7)
2. Check `.indexes <table>` on every table the inserter joins/subselects (T7)
3. Check the table's UNIQUE constraints — single-column PK on multi-tenant table = suspect T3
4. Look for `INSERT OR IGNORE` (T2) and audit whether the unique key includes tenant scope
5. Check verifier's COUNT query — does it have `WHERE project_id = ?`? (T4)

Operator says "stuck for hours / 100% CPU":
1. T7 (correlated subquery, no index) is by far the most common cause
2. FTS5 rebuild on >100k rows can take ~30-60 sec with index, hours without
3. Use `lsof -p <pid>` to confirm DB+journal still being written
4. Use `EXPLAIN QUERY PLAN` on the suspected slow query

Operator says "verify failed with N discrepancies":
1. If most are `skipped_row` → T9 likely (stale skip records)
2. If `count_mismatch` shows central=0 per-project but central has rows → T2/T3
3. If central reports total-rows for every project → T4 (verifier aggregation bug)
