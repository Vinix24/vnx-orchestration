# Task 02 — Tenant-scoped schema migration (composite UNIQUE per ADR-007)

Source-inspiratie: SEOcrawler PR #125 (enable RLS on scan_quota) — local SQLite simulation. Tier: T1 trivial. Deadline: 10 minutes wallclock.

## Context

The seed includes a SQLite database `scan_quota.db` with a table created without tenant-scoping:

```sql
CREATE TABLE scan_quota (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100
);
```

This violates ADR-007 (every central-DB table must have composite UNIQUE/PK over `project_id`). Two tenants currently cannot safely write the same `scan_id`. Your task: write a migration that adds tenant-scoping in a backwards-compatible way.

## Required changes

1. Create `migrate.sql` that:
   - Adds a `project_id TEXT NOT NULL DEFAULT 'default'` column to `scan_quota`
   - Adds a composite UNIQUE constraint over `(project_id, scan_id)`
   - Adds an index on `(project_id)` for tenant-filtered queries
   - Is idempotent — running it twice does not error
2. Apply it cleanly so the existing seed-row stays intact and new rows can be inserted per-tenant without collision.

## Verification

The seed includes 3 rows pre-loaded:
```
(1, 'scan_a', 0, 100)
(2, 'scan_b', 5, 100)
(3, 'scan_c', 10, 100)
```

After your migration:
1. The 3 original rows must remain accessible (their `project_id` must be `'default'`)
2. Inserting `(scan_id='scan_a', project_id='tenant_x')` must succeed (different tenant)
3. Inserting a duplicate `(scan_id='scan_a', project_id='default')` must fail with a UNIQUE constraint violation
4. The composite UNIQUE must use `(project_id, scan_id)` (not just `scan_id`)
5. The index on `(project_id)` must exist

## Files you may create

- `migrate.sql` (create — the migration body)
- `apply_migration.py` (optional — wrapper if you prefer Python over raw `sqlite3` CLI; not required, raw SQL is fine)

Do NOT modify the existing `scan_quota.db` file directly — your migration must be re-runnable from scratch on a fresh copy.

## Definition of done

- `migrate.sql` exists and is valid SQLite SQL
- Running `sqlite3 scan_quota.db < migrate.sql` does not raise
- Schema after migration matches the 5 verification points above
- Migration is idempotent (runs twice without error)
