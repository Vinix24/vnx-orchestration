-- migrate_add_tenant_id.sql
-- Migration audit log and schema setup for add_tenant_id_to_events
--
-- This script creates the audit infrastructure. The actual batched backfill
-- is handled by migrate_add_tenant_id_runner.py for concurrent-write tolerance.
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    batch_count INTEGER DEFAULT 0,
    rows_processed INTEGER DEFAULT 0,
    finished_at TEXT,
    last_error TEXT,
    UNIQUE(migration_name)
);

-- Index for efficient lookup
CREATE INDEX IF NOT EXISTS idx_migration_log_name ON migration_log(migration_name);

-- Verify events table exists
CREATE TABLE IF NOT EXISTS events (
    rowid INTEGER PRIMARY KEY,
    id TEXT,
    data TEXT
);

-- NOTE: The tenant_id column is added by migrate_add_tenant_id_runner.py
-- This ensures idempotency and allows detection of partial migrations.
-- The runner will:
-- 1. Add tenant_id column (nullable) if it doesn't exist
-- 2. Backfill in 10,000-row batches with progress logging
-- 3. Apply NOT NULL constraint after backfill completes
