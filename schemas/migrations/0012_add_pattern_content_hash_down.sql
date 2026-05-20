-- VNX Migration 0012 — DOWN — pattern content_hash rollback
-- Reverses 0012_add_pattern_content_hash.sql: removes content_hash column
-- from success_patterns and drops the associated composite index.
--
-- Pre-down state: success_patterns has content_hash TEXT column +
--   idx_success_patterns_content_hash index + runtime_schema_version row.
-- Post-down state: content_hash column and index removed; version stamp deleted.
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- The index must be dropped before the column drop.
--
-- NOTE: Content hash values are lost on rollback. pattern_dedup will no
-- longer be able to deduplicate patterns by content. Re-run migration 0012
-- and backfill via pattern_dedup.backfill_content_hash() to restore.
--
-- Applied by: operator (manual) — sqlite3 quality_intelligence.db < this_file.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_success_patterns_content_hash;
ALTER TABLE success_patterns DROP COLUMN content_hash;

-- Migration 0012 targets quality_intelligence.db (@db: quality_intelligence).
-- QI uses the schema_version table (TEXT PRIMARY KEY), not runtime_schema_version
-- (which lives in runtime_coordination.db). The _up.sql INSERT into
-- runtime_schema_version was a no-op because that table does not exist in QI;
-- this rollback targets the correct QI versioning table.
DELETE FROM schema_version WHERE version = '12'
    AND description LIKE '%content_hash%';

COMMIT;

PRAGMA foreign_keys = ON;
