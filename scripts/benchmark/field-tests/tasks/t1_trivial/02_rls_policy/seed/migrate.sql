-- Tenant-scoped schema migration per ADR-007.
-- Adds project_id column, composite UNIQUE (project_id, scan_id), and project_id index.
--
-- Idempotency:
--   * Column addition is gated by `sql NOT LIKE '%project_id%'` in the sqlite_master UPDATE.
--   * Both indexes use `CREATE INDEX IF NOT EXISTS`.
--   * Existing rows pick up DEFAULT 'default' for project_id (no data loss).
--
-- The `writable_schema` pragma is the SQLite-canonical pattern for adding a column
-- idempotently in pure SQL (ALTER TABLE ADD COLUMN is not idempotent and SQLite
-- has no `ADD COLUMN IF NOT EXISTS`). `writable_schema = RESET` invalidates the
-- schema cache so the new column is visible to subsequent CREATE INDEX statements
-- in the same connection.
--
-- See https://sqlite.org/pragma.html#pragma_writable_schema

PRAGMA foreign_keys = OFF;

-- Phase 1: add project_id column to scan_quota (idempotent).
PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = 'CREATE TABLE scan_quota (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100,
    project_id TEXT NOT NULL DEFAULT ''default''
)'
WHERE type = 'table'
  AND name = 'scan_quota'
  AND sql NOT LIKE '%project_id%';

PRAGMA writable_schema = RESET;

-- Phase 2: composite UNIQUE (project_id, scan_id) per ADR-007 (idempotent).
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_quota_project_scan
    ON scan_quota (project_id, scan_id);

-- Phase 3: secondary index on project_id for tenant-filtered queries (idempotent).
CREATE INDEX IF NOT EXISTS idx_scan_quota_project
    ON scan_quota (project_id);

PRAGMA foreign_keys = ON;
