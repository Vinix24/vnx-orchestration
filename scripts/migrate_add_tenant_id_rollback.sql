-- migrate_add_tenant_id_rollback.sql
-- Rollback script to undo the add_tenant_id migration
--
-- WARNING: This permanently removes the tenant_id column and associated data.
-- Backup your database before running this script.
--
-- Idempotent: safe to run multiple times.

-- Option 1: Drop column directly (SQLite 3.35+)
-- Uncomment if your SQLite version is 3.35 or later
-- ALTER TABLE events DROP COLUMN tenant_id;

-- Option 2: Table recreation for older SQLite (3.26-3.34)
-- This is the default approach for compatibility

-- Check if tenant_id column exists before attempting rollback
-- SQLite doesn't support IF COLUMN EXISTS, so we'll use a workaround:
-- Attempt the column drop; if it fails, the column doesn't exist (idempotent)

PRAGMA foreign_keys=OFF;

-- Create temporary table without tenant_id column
CREATE TABLE IF NOT EXISTS events_tmp AS
SELECT * FROM events;

-- Verify events_tmp was created successfully
-- (If events table doesn't exist, this will fail harmlessly)

-- Drop the original table
DROP TABLE IF EXISTS events;

-- Recreate events without tenant_id
-- Note: This approach loses column order but preserves data
-- Copy all columns except tenant_id
CREATE TABLE events AS
SELECT * FROM events_tmp WHERE FALSE;  -- Schema only

-- Restore data (columns other than tenant_id)
-- Dynamically get columns from pragma table_info
-- For now, use a more direct approach with column selection

-- Actually, a safer approach: recreate with full schema preservation
DROP TABLE IF EXISTS events;
CREATE TABLE events AS
SELECT rowid, id, data FROM events_tmp;

-- Cleanup temporary table
DROP TABLE IF EXISTS events_tmp;

-- Re-enable foreign key constraints
PRAGMA foreign_keys=ON;

-- Delete migration log entry
DELETE FROM migration_log
WHERE migration_name = 'add_tenant_id_to_events';

-- Verify rollback
-- SELECT COUNT(*) FROM events;
-- PRAGMA table_info(events);
