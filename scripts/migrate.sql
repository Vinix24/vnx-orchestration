-- ADR-007 tenant-scoping migration for scan_quota
-- Adds project_id column, composite UNIQUE (project_id, scan_id), and project_id index.
--
-- Idempotent in pure SQLite SQL:
--   * Column addition uses PRAGMA writable_schema gated on NOT LIKE '%project_id%'
--   * Indexes use CREATE ... IF NOT EXISTS
--   * Safe to run via: sqlite3 scan_quota.db < scripts/migrate.sql
--
-- For the rationale behind writable_schema (canonical SQLite pattern for
-- idempotent column addition), see:
--   https://sqlite.org/pragma.html#pragma_writable_schema

PRAGMA foreign_keys = OFF;

-- Phase 1: Add project_id column to scan_quota (idempotent).
-- Existing rows get project_id = 'default' via the column DEFAULT.
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

-- Phase 2: Composite UNIQUE (project_id, scan_id) per ADR-007.
CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_quota_project_scan
    ON scan_quota(project_id, scan_id);

-- Phase 3: Index on project_id for tenant-filtered queries.
CREATE INDEX IF NOT EXISTS idx_scan_quota_project_id
    ON scan_quota(project_id);

PRAGMA foreign_keys = ON;
