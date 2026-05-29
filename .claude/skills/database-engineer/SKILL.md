---
name: database-engineer
description: SQLite database expertise for migrations, multi-tenant patterns, transactional safety, FTS5 virtual tables, and schema evolution. Use this skill for ANY work involving schema changes, data migrations, INSERT/UPSERT semantics, FTS5 rebuilds, transaction rollback design, multi-tenant data isolation, or SQLite performance tuning. Triggers include: writing migration SQL, modifying `_import_table`-style importers, designing UNIQUE/PK constraints, adding indexes for JOINs, debugging "rows missing" / "wrong count" / "stuck for hours" symptoms in SQLite-backed systems.
allowed-tools: [Read, Write, Edit, MultiEdit, Bash, Grep, Glob]
paths: ["schemas/**", "scripts/**", "tests/**"]
---

# Database Engineer

SQLite-domain specialist for migrations and data integrity.

## Core Mission

Migrations are integration code. Most "data" bugs aren't algorithmic — they're contract bugs between schema, importer, and verifier. This skill exists because P4 (the VNX one-shot consolidation migrator) consumed **5 fix-forward rounds** before converging, and the bugs were almost entirely SQLite/multi-tenant gotchas a generalist would miss.

The specific recurring failure-modes are catalogued in `references/bug-taxonomy.md` with concrete VNX examples. Read it before writing any migration code.

## When to Use

- Writing or editing files under `schemas/migrations/`
- Touching `_import_table`, `_compare_counts`, or any code that ATTACHes / SELECTs / INSERTs across tenant boundaries
- Designing UNIQUE constraints or primary keys for any table that will hold multi-project rows
- Adding `JOIN` or correlated-subquery code that scans tables ≥10k rows
- Debugging a migration that "completed" but produced wrong row counts
- Operator complains "stuck at 100% CPU for hours" — likely O(N×M) (see `references/sqlite-fts5-gotchas.md`)
- Any code that calls `INSERT OR IGNORE` or `INSERT OR REPLACE`

## STEP 0 — Foundational Check (Mandatory)

BEFORE proposing any design, fix, or implementation:

1. **Consult relevant ADRs** in `docs/governance/decisions/`. Special attention to:
   - ADR-005 (NDJSON audit ledger as primary observability)
   - ADR-007 (multi-tenant project_id stamping; composite keys for central state DBs)
   - ADR-010 (subprocess adapter as canonical Claude routing)
   List any ADR that applies to the task and how it constrains your solution.

2. **Consult relevant memory** in `~/.claude/projects/-Users-vincentvandeth-Development-vnx-dev-githost/memory/MEMORY.md` — particularly entries about past architectural incidents.

3. **Check P4-style incident docs** in `claudedocs/` for analogous failures (e.g., `2026-05-09-p4-migration-architecture-lessons.md` for multi-tenant migration patterns).

4. **State your foundational read aloud** at the start of your response. Example: "ADR-007 applies: new tabel X needs composite PK over project_id. Per P4 §4.2, single-column UNIQUE is a smell. Memory [[adr-007-multitenant-composite-keys]] confirms."

Skipping STEP 0 is a process violation, not a shortcut. The FUT-1 chain (2026-05-28) burned 6 codex rounds because ADR-007 was not consulted at design time.

## Decision Protocol

Before writing migration code, walk through `references/migration-defense-checklist.md` (loaded explicitly when needed; checklist is reproduced inline below for fast access).

When asked to fix a "data missing" or "data wrong" bug, the first investigative step is `EXPLAIN QUERY PLAN` on the failing query and `.indexes` on the related tables. Two-thirds of the bugs in the P4 history were diagnosable this way in under five minutes once anyone thought to look.

## 1. SQLite Mechanics

### Journal modes
SQLite supports two journal modes operators commonly hit: `DELETE` (rollback journal as a sibling file `<db>-journal`) and `WAL` (`<db>-wal` + `<db>-shm`). Never copy a DB file mid-write without one of:

- `sqlite3.Connection.backup(dest)` — handles both modes correctly, locks briefly, copies all pages including in-flight ones
- `BEGIN IMMEDIATE; .dump | sqlite3 newfile; COMMIT;` — slower but produces SQL text
- For WAL specifically: `PRAGMA wal_checkpoint(TRUNCATE)` flushes the WAL into the base file, then a normal file copy is consistent

`shutil.copy2(src, dst)` of a WAL DB is **not safe**: committed state may live in `-wal` and the copy will be partial. R2 of P4 hit this; fixed by switching to `Connection.backup()`.

### Hot journal recovery
If a writer process is killed mid-transaction, the journal/WAL file remains. The next process to open the DB reads it and replays/rolls back. This is automatic but has two implications:

1. After SIGTERM/SIGKILL of a migrator, **open the DB once** (e.g. `sqlite3 <db> "SELECT 1;"`) before inspecting it. Otherwise the file size you see includes the in-flight transaction's pages and reads can be confusing.
2. Pre-snapshot files (`<db>.presnap.*`) created by `_snapshot_central` are belt-and-suspenders on top of journal recovery, useful when the rollback semantics need to span multiple migrations rather than a single transaction. Don't delete them while a migrator is alive.

### FTS5 virtual tables
FTS5 is a separate physical store with its own segments. Schema-altering it usually means **drop + recreate + re-INSERT-from-temp-table**. There is no `ALTER TABLE … ADD COLUMN` for FTS5 vtabs; column changes require rebuild. See `references/sqlite-fts5-gotchas.md` for the rebuild pattern and the perf trap that almost shipped in P4 round 4.

### ATTACH DATABASE for multi-source migrations
Read-only attach uses a URI:
```python
con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
```
The `mode=ro` flag is critical — without it, the attach opens a write connection that briefly modifies the source's `-shm` file and breaks the read-only contract (operator backups can fail integrity checks).

## 2. Multi-Tenant Patterns

In a single-tenant database, `id INTEGER PRIMARY KEY` is fine. In a multi-tenant central DB, that same constraint causes silent data loss the moment two tenants happen to have overlapping `id` values. The P4 chain shipped this bug twice (in `terminal_leases` and `execution_targets`).

### Composite uniqueness is the default
For any table that will hold rows from multiple tenants, the unique constraints should be `(project_id, <natural_key>)`, not `<natural_key>` alone. Examples that bit P4:
- `terminal_leases.terminal_id` UNIQUE → must be `(project_id, terminal_id)` UNIQUE
- `execution_targets.target_key` UNIQUE → composite
- `tag_combinations.tag_tuple` UNIQUE → composite
- `code_snippets` FTS5 row identity is `rowid` + `project_id` column; queries must always filter on `project_id`

### Stamping vs deriving project_id
Two patterns coexist in VNX:

1. **Stamp at import**: importer overwrites the `project_id` column with the runtime project's id, regardless of what the source row contained. Used by `_import_table`. Risk: silent skip on PK conflict (see T2 in bug taxonomy).
2. **Stamp at write-time**: writer code (e.g. `dispatch_register`, `append_receipt`) takes a `project_id` parameter and writes it explicitly. Used by per-project state code. Risk: callers forget; helper enforcement matters.

Prefer pattern 2 for new code. When migrating legacy single-tenant data, pattern 1 is unavoidable — pair it with `ON CONFLICT … DO UPDATE SET project_id=excluded.project_id` so re-stamping wins on collision (see Section 3).

### Verifier scoping
Per-project verification queries MUST include `WHERE project_id = ?`. The P4 verifier had a bug where `code_snippets` count was reported as the global total for every project; this only manifests because of FTS5 vtab semantics but the lesson is broader: **never aggregate without scope** in a multi-tenant verifier.

## 3. Migration Safety

### UPSERT vs INSERT OR IGNORE
This decision shipped wrong in P4. Cheat sheet:

| Situation | Use |
|---|---|
| Re-running migration; rows might already exist; want to skip duplicates | `INSERT OR IGNORE` **only if** PK includes tenant scope |
| Same as above but PK is single-column | `INSERT … ON CONFLICT(<pk>) DO UPDATE SET <safe_cols>=excluded.<safe_cols>` (UPSERT) — be explicit about which columns are safe to overwrite |
| Multiple tenants might have same PK natural key | Composite `(project_id, key)` UNIQUE; `INSERT OR IGNORE` becomes safe again |
| Want strict "fail if duplicate" | Plain `INSERT` — sqlite raises `IntegrityError` |
| Replacing entire row | `INSERT OR REPLACE` — but this **deletes-then-inserts**, breaking FK CASCADE in surprising ways |

**Default for migration code: composite UNIQUE + `INSERT OR IGNORE`.** It's the simplest invariant: each tenant's row is uniquely identified, retries are no-ops, and there's no "stale row wins" surprise.

### Idempotency
A migrator that can be safely re-run is worth orders of magnitude more than one that requires careful manual cleanup on every retry. Idempotency means:

- Each row has an `imported_at` or `run_id` so repeat imports are detectable
- A bookkeeping table like `p4_import_idempotency(project_id, source_table, source_rowid)` records what's been done
- Verification looks at "this run's results" not "all results" — see T9 in bug taxonomy for what happens when run_id scoping is missing

### Schema-first vs imperative migrations
The P4 design uses ALTER-TABLE migrations layered on whatever central schema happened to exist. This works for incremental changes but fragile for greenfield (`--fresh-central` had to be added in R3 because empty central produced empty migration runs).

For new migrators, prefer **explicit central schema file** + descriptor-driven importer — see `references/multi-tenant-patterns.md` Section 3 for the pattern.

### Rollback semantics
SQLite's transaction rollback (`BEGIN`/`ROLLBACK`) is automatic on Python `sqlite3.Connection` exceptions. But cross-DB rollback (e.g. central QI + central RC) requires explicit coordination — the P4 pattern uses `_snapshot_central` to create `<db>.presnap.<phase>.<pid>` files before risky migrations, then restores them on failure. This is correct only if every writer respects the contract.

## 4. Performance

### Indexes for JOINs and correlated subqueries
The R4 perf bug: migration `0016_rebuild_fts5.sql` had:
```sql
INSERT INTO code_snippets (...)
SELECT ..., COALESCE(
  (SELECT project_id FROM snippet_metadata m
   WHERE m.snippet_rowid = code_snippets_rebuild_tmp.rowid),
  'vnx-dev'
)
FROM code_snippets_rebuild_tmp;
```
`snippet_metadata.snippet_rowid` was not indexed. For 727k snippets × 119k metadata rows = ~87B row scans = ~3-5h ETA.

Fix: add `CREATE INDEX IF NOT EXISTS idx_snippet_metadata_rowid ON snippet_metadata(snippet_rowid);` **before** the rebuild SELECT. Reduces to O(N log M) ≈ 17M ops, completes in seconds.

**Rule:** any migration with `JOIN` or correlated subquery involving a table ≥10k rows MUST include the supporting index in the same migration file.

### EXPLAIN QUERY PLAN
Always run before shipping a migration that touches large tables:
```sql
EXPLAIN QUERY PLAN <your-INSERT/SELECT>;
```
Look for `SCAN TABLE` (bad for inner loops). Want to see `SEARCH TABLE … USING INDEX …`.

### FTS5 rebuild costs at scale
~5k rows/sec on a modern SSD, dominated by tokenizer work. For 727k rows expect ~150 sec on the rebuild alone, plus journal write overhead (~30% extra). If observed >10 min for ~1M rows, suspect missing index on subquery columns.

## 5. Verifier Patterns

A verifier that shares code paths with the importer is structurally co-blind: any helper bug affects both, neither catches the other. Three rules:

1. **Different connection / different process**: open the central DB in a fresh `sqlite3.Connection`, ideally via a different code module. The P4 verifier opens via `_compare_counts` which reuses importer helpers; this is why R5 shipped a verifier-aggregate bug.
2. **Per-project scoped queries**: every COUNT must filter by `project_id`. A "central total" check is fine as a *separate* check, not as the per-project verification.
3. **Fail-fast on missing scope**: if a table is expected to have `project_id` and it doesn't, the verifier must raise (or write to `read_errors`) — not silently fall back to global COUNT(*). The P4 R5 fix made `_compare_counts` strict about this.

## 6. Migration Defense Checklist (mandatory pre-merge)

Apply each item before merging migration code. Each line corresponds to a real bug from the P4 history.

- [ ] **Composite UNIQUE on multi-tenant tables**: every imported table with a natural key has `UNIQUE(project_id, key)` not `UNIQUE(key)` alone (T3, T6 in `references/bug-taxonomy.md`)
- [ ] **UPSERT chosen, not IGNORE**: `INSERT OR IGNORE` only used where the unique constraint already includes `project_id`; otherwise switch to `ON CONFLICT … DO UPDATE` (T2)
- [ ] **Verifier filters per-project**: every COUNT/checksum query in the verifier has `WHERE project_id = ?`; no aggregate fallback (T4)
- [ ] **Bootstrap path tested empty**: there is a test where central DB file does not exist at start, and the migration produces a valid central with all expected tables and zero data (T5)
- [ ] **DEFAULT used only for NOT NULL backfill**: any `DEFAULT '<value>'` on a column added by ALTER must be reasoned about in the importer's stamp logic. If the default is "vnx-dev" or another sentinel that could mask un-stamped rows, add a NOT NULL CHECK in the same migration that forbids the sentinel post-migration (T1)
- [ ] **Indexes for JOIN/correlated columns**: any migration with `JOIN` or `(SELECT … WHERE x = outer.y)` against a table ≥10k rows includes the supporting `CREATE INDEX` (T7)
- [ ] **Real-data fixture test**: there is at least one integration test that runs the migration against fixtures sized within 1 OOM of production data; not just synthetic 5-row examples (Section 3.2 of lessons doc)
- [ ] **Read failures distinguishable**: source-table-missing and source-table-unreadable are separate codes in the importer's `read_errors`; never silently degrade to count=0 (T8)
- [ ] **run_id scoping on retryable records**: any `<feature>_skipped` / `<feature>_failed` table includes `run_id` and `resolved_at` to filter cross-run state (T9)
- [ ] **WAL-safe snapshots**: pre-migration snapshots of source DBs use `Connection.backup()` or `wal_checkpoint(TRUNCATE) + cp`, not bare `shutil.copy2` (T10)

## Reference Files

- `references/bug-taxonomy.md` — the 10 recurring patterns with VNX examples; READ FIRST when triggered
- `references/sqlite-fts5-gotchas.md` — FTS5 rebuild patterns + perf traps
- `references/multi-tenant-patterns.md` — composite key design, stamping strategies, descriptor-driven importers
- `references/p4-postmortem-summary.md` — links to full lessons doc + key code:line citations

## Companion Skills

- For VNX-specific schema knowledge (which tables hold what, the dispatch lifecycle, intelligence-injection flow): use `intelligence-engineer` instead. This skill is the *generic* SQLite layer; `intelligence-engineer` is the *VNX domain* layer.
- For general code review: `reviewer` may request a `database-engineer` second-opinion review when a PR touches `schemas/`, `_import_table`-style code, or migration files.

## Codex Defense Checklist (inherited from backend-developer)

Database work still ships through the same code-review pipeline. The patterns in `backend-developer/SKILL.md` Section "Codex Defense Checklist" apply: atomic writes, fcntl locking on shared NDJSON, null guards, schema version checks. Treat that as a baseline and the Migration Defense Checklist above as additive for any migration-touching work.
