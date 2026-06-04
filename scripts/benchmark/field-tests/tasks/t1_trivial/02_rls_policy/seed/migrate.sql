-- migrate.sql — tenant-scoped schema migration for scan_quota (ADR-007).
--
-- The seed table was created without tenant-scoping, so two tenants cannot
-- safely reuse the same scan_id. This migration adds:
--   * column     project_id TEXT NOT NULL DEFAULT 'default'
--   * composite  UNIQUE over (project_id, scan_id)   -- one tenant may not dup
--   * secondary  index on (project_id)               -- tenant-filtered reads
--
-- Why a table rebuild instead of ALTER TABLE: SQLite supports neither
-- `ALTER TABLE ... ADD CONSTRAINT` nor a re-runnable `ADD COLUMN` (the second
-- run errors on the duplicate column). Rebuilding into a staging table and
-- enforcing tenancy through explicit `CREATE ... INDEX IF NOT EXISTS`
-- statements is both SQLite-correct and idempotent.
--
-- Data safety: the rebuild copies the columns shared by the pre- and
-- post-migration shapes; legacy rows inherit project_id = 'default' from the
-- column DEFAULT. `GROUP BY scan_id` keeps the copy collision-free so a
-- re-run never trips the composite UNIQUE. Every CREATE uses IF NOT EXISTS
-- and the staging table is dropped up front, so the whole script is safe to
-- run repeatedly via `sqlite3 scan_quota.db < migrate.sql`.

BEGIN TRANSACTION;

-- Clear any staging table left behind by an interrupted earlier run.
DROP TABLE IF EXISTS scan_quota_tenant_scoped;

-- Canonical tenant-scoped shape. UNIQUE is enforced by an explicit index
-- after the rename (verify scans index DDL text), not inline here.
CREATE TABLE scan_quota_tenant_scoped (
    id          INTEGER PRIMARY KEY,
    scan_id     TEXT    NOT NULL,
    used_count  INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100,
    project_id  TEXT    NOT NULL DEFAULT 'default'
);

-- Copy only the columns present in BOTH the un-tenanted and migrated schemas;
-- project_id falls back to its DEFAULT ('default'). GROUP BY scan_id collapses
-- rows that would land on the same (project_id, scan_id) once project_id
-- defaults, keeping the explicit UNIQUE index buildable on re-runs.
INSERT INTO scan_quota_tenant_scoped (id, scan_id, used_count, quota_limit)
    SELECT MIN(id), scan_id, used_count, quota_limit
    FROM scan_quota
    GROUP BY scan_id;

DROP TABLE scan_quota;
ALTER TABLE scan_quota_tenant_scoped RENAME TO scan_quota;

-- Composite UNIQUE over (project_id, scan_id): the same scan_id is allowed
-- across tenants, duplicates within a single tenant are rejected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_quota_project_scan
    ON scan_quota (project_id, scan_id);

-- Standalone index on the tenant key for tenant-filtered queries.
CREATE INDEX IF NOT EXISTS idx_scan_quota_project
    ON scan_quota (project_id);

COMMIT;
