-- VNX Migration 2026_05_intelligence_ab_arm -- DOWN -- A/B arm rollback
-- Reverses 2026_05_intelligence_ab_arm.sql: drops idx_injection_ab_arm index,
-- removes ab_arm column from intelligence_injections, deletes v16 stamp.
--
-- Pre-down state (v16): intelligence_injections.ab_arm TEXT column present
--   + idx_injection_ab_arm index.
-- Post-down state (v15): ab_arm column and index removed; v16 stamp deleted.
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- Index must be dropped before column drop.
--
-- NOTE: ab_arm values ('treatment'|'control') are permanently lost on rollback.
-- A/B random-skip tracking in intelligence_selector will stop working until
-- the column is re-added by re-running the up-migration (apply_ab_arm.py).
--
-- Applied by: operator (manual) — sqlite3 runtime_coordination.db < this_file.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_injection_ab_arm;
ALTER TABLE intelligence_injections DROP COLUMN ab_arm;

DELETE FROM runtime_schema_version WHERE version = 16;

COMMIT;

PRAGMA foreign_keys = ON;
