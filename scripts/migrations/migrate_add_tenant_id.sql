-- Migration: Add tenant_id column to events table
-- Purpose: Multi-tenant isolation with audit trail
-- Requirements: idempotent, concurrent-write safe, rollback capable

-- ============================================================================
-- Migration Infrastructure: Audit Trail
-- ============================================================================

CREATE TABLE IF NOT EXISTS migration_log (
    migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_name TEXT NOT NULL UNIQUE,
    start_timestamp TEXT NOT NULL,
    end_timestamp TEXT,
    batch_count INTEGER DEFAULT 0,
    total_rows_migrated INTEGER DEFAULT 0,
    status TEXT DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'completed', 'failed')),
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Index for fast status checks
CREATE INDEX IF NOT EXISTS idx_migration_log_status ON migration_log(migration_name, status);

-- ============================================================================
-- ADD COLUMN: Idempotent DDL (applied by runner, not here)
-- ============================================================================

-- This would be executed by Python runner:
-- ALTER TABLE events ADD COLUMN tenant_id INTEGER;

-- ============================================================================
-- NOT NULL CONSTRAINT: Applied after backfill completes
-- ============================================================================

-- This would be executed by Python runner after all rows backfilled:
-- ALTER TABLE events ALTER COLUMN tenant_id SET NOT NULL;
-- (Requires SQLite 3.26+; for older versions, use manual table recreation)

-- ============================================================================
-- ROLLBACK SCRIPT: Undo migration
-- ============================================================================

-- Migration rollback (run separately with runner validation):
-- 1. Check that no active sessions depend on tenant_id
-- 2. Drop constraint (if added)
-- 3. Remove column
-- 4. Log rollback in migration_log

-- Placeholder for rollback entry:
-- INSERT INTO migration_log (migration_name, status, error_message)
-- VALUES ('add_tenant_id_to_events_rollback', 'in_progress', NULL);

-- ============================================================================
-- HELPER QUERIES: Used by Python runner for state and resumption
-- ============================================================================

-- Check if migration is already completed (idempotency gate):
-- SELECT COUNT(*) as completed FROM migration_log
-- WHERE migration_name = 'add_tenant_id_to_events'
-- AND status = 'completed';

-- Get current migration status for resumption:
-- SELECT batch_count, total_rows_migrated FROM migration_log
-- WHERE migration_name = 'add_tenant_id_to_events'
-- AND status IN ('in_progress', 'completed')
-- ORDER BY migration_id DESC LIMIT 1;

-- Count remaining NULL rows:
-- SELECT COUNT(*) as remaining FROM events WHERE tenant_id IS NULL;

-- Count total rows in events table:
-- SELECT COUNT(*) as total FROM events;
