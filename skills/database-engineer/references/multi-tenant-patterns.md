# Multi-Tenant Database Patterns

How to design SQLite schemas and migrations that hold rows from multiple tenants safely.

## Core insight

Single-tenant SQLite design produces three patterns that fail under multi-tenancy:

1. `id INTEGER PRIMARY KEY` — guaranteed to collide between tenants
2. `UNIQUE(natural_key)` — also collides; semantics differ between tenants
3. `INSERT OR IGNORE` on conflict — the stale tenant's row wins, the new one is silently dropped

The fix is structural: every table that will hold multi-tenant rows declares its uniqueness and identity in terms of `(project_id, …)` from the start. Retrofitting this onto a single-tenant schema is exactly what P4 attempted, with mixed success.

## Pattern 1: Composite primary key

For tables that don't have a natural compound key, the pragmatic default:

```sql
CREATE TABLE terminal_leases (
    project_id  TEXT NOT NULL,
    source_rowid INTEGER NOT NULL,
    terminal_id TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'idle',
    -- ... other columns ...
    PRIMARY KEY (project_id, source_rowid)
);
CREATE INDEX idx_terminal_leases_terminal ON terminal_leases(project_id, terminal_id);
```

`source_rowid` is the original tenant-side `id`. `(project_id, source_rowid)` is unique by construction. Queries that need by-terminal lookup get an explicit index, also project-scoped.

Migration safety: `INSERT OR IGNORE` is now safe because the conflict key includes `project_id`. Two tenants writing the same `source_rowid=1` produce two separate central rows.

## Pattern 2: Composite unique on natural key

Some tables have a meaningful natural key — `terminal_id`, `dispatch_id`, `tag_tuple`. Don't drop those uniqueness guarantees; project-scope them:

```sql
CREATE TABLE terminal_leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    terminal_id TEXT NOT NULL,
    -- ... other columns ...
    UNIQUE (project_id, terminal_id)
);
```

The auto-increment `id` provides a stable rowid for FK references; the composite UNIQUE prevents intra-project duplicate keys but allows cross-project overlap.

This pattern was the P4 round-5 fix for `terminal_leases`, `execution_targets`, and `tag_combinations`.

## Pattern 3: Identifier prefix-rewrite

For columns that are CROSS-references rather than primary identity (e.g. `coordination_events.entity_id` references `dispatches.dispatch_id`), there's a different fix: prefix the value at import time so two tenants' `dispatch_id="2026-05-01-foo"` become `"vnx-orchestration:2026-05-01-foo"` and `"sales-copilot:2026-05-01-foo"` in central, never colliding.

P4 implements this via `_prefix_value(project_id, value)`. The list of prefix-target columns is in `COLLISION_PREFIX_COLUMNS` and `_collect_collision_columns` (schema-driven extension).

When to prefer prefix-rewrite over composite uniqueness:

| Use prefix-rewrite when | Use composite UNIQUE when |
|---|---|
| The column is a foreign-key-like reference, not a primary identity | The column IS the primary identity |
| Two tenants might legitimately have the same value with different meanings | Same |
| The column appears in JSON arrays (`source_dispatch_ids`) | The column is a single scalar |
| Downstream queries treat the column as a global id | Downstream queries always include `project_id` in the WHERE |

## Pattern 4: project_id stamping at write-time

For NEW code (not retrofitted migrations), the cleanest design is: every writer takes `project_id` explicitly, every helper enforces it, and the column has NO DEFAULT (NOT NULL with no default → required).

```python
def append_dispatch(con, project_id: str, dispatch_id: str, ...):
    if not project_id:
        raise ValueError("project_id required")
    con.execute(
        "INSERT INTO dispatches (project_id, dispatch_id, ...) VALUES (?, ?, ...)",
        (project_id, dispatch_id, ...)
    )
```

This pattern has near-zero failure modes once enforced. Compare to the retrofit pattern (column with DEFAULT, importer overrides) which has at least three (T1, T2, T3 in the bug taxonomy).

## Pattern 5: Schema-first with descriptors

For migrators that consolidate multiple sources, the most robust architecture is:

1. **Authoritative central schema** in a single SQL file: `schemas/central_v1.sql`. All tables defined explicitly, with project-scoped uniqueness baked in.
2. **Per-source mapping descriptors** as data: `mappings/source_<id>.yaml` describing how source columns map to central columns, what transformations apply (project_id stamping, prefix-rewrite, JSON array unpack).
3. **Generic importer engine** that reads descriptors and central schema, executes the mapping. No per-source code.
4. **Verifier reads the SAME descriptors** to compute expected per-project counts. Reusing the descriptor source-of-truth removes the importer/verifier co-blindness (T4).

P4 doesn't follow this pattern (the importer is per-table imperative). For new migration tools, prefer this design.

## Default values: when they bite

`DEFAULT '<value>'` on a column added by ALTER TABLE is fine for backfill purposes. It bites when:

- The default is a sentinel that doubles as a real value (`'vnx-dev'` is both "no project specified" AND "the actual vnx-dev project")
- Importer logic has a code path that doesn't override the default — the default leaks through as legitimate data
- Verifier doesn't distinguish "row stamped vnx-dev intentionally" from "row stamped vnx-dev because not stamped at all"

**Rules of thumb:**
- For NOT NULL backfill ONLY: use a sentinel that is GUARANTEED INVALID (`'__UNMIGRATED__'`) and add a CHECK constraint that the post-migration code can't write that value
- If the column needs a real default (e.g. `state TEXT DEFAULT 'idle'`), make sure no other code path treats `'idle'` as "no state set"
- For `project_id` specifically: NEVER default to a real project name. Default to `__UNSET__` and CHECK that no row has that value after migration completes

## Verifier independence

Three rules so the verifier doesn't share importer bugs:

1. **Different connection**: Don't use the importer's `sqlite3.Connection` for verification; open a fresh one. Helps with WAL/cache state.
2. **Different code path**: The verifier shouldn't import `_import_table` or any helper that the importer also uses. The whole point is to catch bugs in those helpers.
3. **Independent data sources**: The verifier should re-query SOURCE DBs (via fresh ATTACH) and recompute expected counts/checksums; not just count central. If both source-recount and central-count come from the same data flow, you're not verifying anything.

For very-high-stakes migrations (P5+ if it happens), consider a SEPARATE PROCESS verifier — a different Python invocation entirely, communicating only via JSON output. Hardest possible separation.

## Practical migration steps for retrofitting single-tenant → multi-tenant

1. Add `project_id TEXT` column with `__UNSET__` sentinel default. Don't make it NOT NULL yet.
2. In the same migration, backfill `project_id = 'the-only-tenant-we-have-now'` for all existing rows. NOW make it NOT NULL.
3. Add CHECK constraint forbidding `__UNSET__` (catch any path that bypasses the backfill).
4. Drop single-column UNIQUE constraints that should be project-scoped. Recreate as composite UNIQUE with `project_id`.
5. Update all callers of write helpers to require `project_id` parameter.
6. Update all read queries to include `WHERE project_id = ?` or `JOIN ... ON project_id`.
7. Audit FK references for cross-project pollution. Use prefix-rewrite if needed.
8. Add a verifier that runs at startup and asserts no `__UNSET__` rows exist; fail loudly if found.

This is more steps than P4 took, which is exactly why P4 needed 5 fix-forward rounds.
