-- SUPERSEDED by 0038_intelligence_injections_ab_arm.sql (paired runner apply_0038.py), which adds
-- ab_arm and ab_window under user_version 38. Kept as history; nothing here executes on a new store.
-- Its down file (2026_05_intelligence_ab_arm_down.sql) is never run on a store with
-- user_version >= 38: it deletes a v16 stamp that was never there and drops ab_arm under a v38 stamp.
-- Use apply_0038.revert_migration instead.
--
-- Migration: A/B framework — ab_arm column on intelligence_injections
-- Adds ab_arm TEXT column ('treatment'|'control') for A/B random-skip tracking.
--
-- Target DB: runtime_coordination.db (intelligence_injections table)
-- Applied by: Python runner (idempotent PRAGMA check before ALTER TABLE)
--   python3 scripts/lib/migrations/apply_ab_arm.py --db <path/to/runtime_coordination.db>
--
-- Idempotency: Python runner checks PRAGMA table_info(intelligence_injections)
-- for 'ab_arm' before issuing ALTER TABLE. SQLite < 3.37 does not support
-- ADD COLUMN IF NOT EXISTS directly.
--
-- After Python adds the column, this SQL handles index + version stamp.
-- Tested by: tests/test_intelligence_ab_framework.py
-- Post-migration version: 16

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- Note: ALTER TABLE intelligence_injections ADD COLUMN ab_arm TEXT DEFAULT 'treatment';
-- is executed by the Python runner prior to this script, not inline here.

CREATE INDEX IF NOT EXISTS idx_injection_ab_arm
    ON intelligence_injections (ab_arm);

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (16, 'intelligence_injections.ab_arm: A/B random-skip framework (V5)');

COMMIT;

PRAGMA foreign_keys = ON;
