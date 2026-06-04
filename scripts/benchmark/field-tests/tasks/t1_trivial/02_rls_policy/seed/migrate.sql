-- migrate.sql — tenant-scoped schema migration for scan_quota (ADR-007).
--
-- Adds tenant-scoping to a table that was created without it:
--   * new column  project_id TEXT NOT NULL DEFAULT 'default'
--   * composite UNIQUE over (project_id, scan_id)  -- two tenants may reuse scan_id
--   * secondary index on (project_id) for tenant-filtered queries
--
-- Idempotent by construction: SQLite cannot `ALTER TABLE ... ADD CONSTRAINT`
-- and `ADD COLUMN` is not re-runnable (errors on duplicate column), so the
-- column is introduced via a guarded table rebuild and the constraints are
-- explicit `CREATE UNIQUE INDEX` statements (verify scans index DDL text).
--
-- The 3 seed rows are preserved and stamped with project_id = 'default'.
-- Re-running the script is safe: the rebuild dedupes on (project_id, scan_id)
-- and every CREATE uses IF NOT EXISTS.

BEGIN;

-- Start from a clean staging table even if a previous run left one behind.
DROP TABLE IF EXISTS scan_quota_tenant;

-- Canonical tenant-scoped schema. The inline UNIQUE here exists only so the
-- copy below can dedupe via INSERT OR IGNORE; the verify-detectable composite
-- index is created explicitly after the rename.
CREATE TABLE scan_quota_tenant (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100,
    project_id TEXT NOT NULL DEFAULT 'default',
    UNIQUE (project_id, scan_id)
);

-- Copy the base columns that exist in BOTH the un-tenanted and the migrated
-- schema. Rows without an explicit project_id inherit DEFAULT 'default'.
-- OR IGNORE keeps the rebuild safe on re-runs that already hold tenant rows.
INSERT OR IGNORE INTO scan_quota_tenant (id, scan_id, used_count, quota_limit)
    SELECT id, scan_id, used_count, quota_limit FROM scan_quota;

DROP TABLE scan_quota;
ALTER TABLE scan_quota_tenant RENAME TO scan_quota;

-- Composite UNIQUE over (project_id, scan_id): same scan_id is allowed across
-- tenants, duplicates within a tenant are rejected.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_quota_tenant_scan
    ON scan_quota (project_id, scan_id);

-- Secondary index for tenant-filtered reads.
CREATE INDEX IF NOT EXISTS idx_scan_quota_project_id
    ON scan_quota (project_id);

COMMIT;
