-- VNX Migration 0011 — DOWN — pattern_category rollback
-- Reverses 0011_add_pattern_category.sql: removes pattern_category column from
-- success_patterns and antipatterns; drops associated indexes.
--
-- Pre-down state: both tables have pattern_category TEXT NOT NULL DEFAULT 'code'
--   (antipatterns: DEFAULT 'antipattern_evidence') + category indexes.
-- Post-down state: pattern_category column and indexes removed from both tables.
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- Indexes must be dropped before column drops.
--
-- NOTE: Backfilled category values ('governance', 'process', 'code') are lost.
-- Diversity rules in the intelligence selector that depend on pattern_category
-- will break; revert selector code before rolling back this migration.
--
-- Applied by: operator (manual) — sqlite3 quality_intelligence.db < this_file.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- Drop indexes first (cover only pattern_category columns)
DROP INDEX IF EXISTS idx_success_patterns_pattern_category;
DROP INDEX IF EXISTS idx_antipatterns_pattern_category;

-- Remove pattern_category column from both tables (SQLite 3.35+)
ALTER TABLE success_patterns DROP COLUMN pattern_category;
ALTER TABLE antipatterns DROP COLUMN pattern_category;

COMMIT;

PRAGMA foreign_keys = ON;
