-- VNX Migration 0010 — DOWN — hot-table project_id rollback
-- Reverses 0010_add_project_id.sql: removes project_id column from 8 QI
-- tables and 6 RC tables (the Phase 0 hot-table extension).
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- This is the most foundational rollback — only apply if you are also
-- rolling back 0011 through 0020 first (prerequisite chain).
--
-- Pre-down state: 14 hot tables have project_id column + per-table index.
-- Post-down state: project_id and indexes removed; runtime_schema_version v10 deleted.
--
-- CAUTION: Rolling back this migration destroys multi-tenant data segregation.
-- All data that was imported per-project becomes unattributed. This is a
-- last-resort operation — restore from backup instead when possible.
--
-- Section order matters:
--   Apply the QI section to quality_intelligence.db
--   Apply the RC section to runtime_coordination.db
-- Do NOT apply both sections to the same database.
--
-- Applied by: operator (manual) — apply correct section to correct DB.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- @db: quality_intelligence — 8 hot tables
-- ============================================================================

DROP INDEX IF EXISTS idx_success_patterns_project;
ALTER TABLE success_patterns DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_antipatterns_project;
ALTER TABLE antipatterns DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_prevention_rules_project;
ALTER TABLE prevention_rules DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_pattern_usage_project;
ALTER TABLE pattern_usage DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_confidence_events_project;
ALTER TABLE confidence_events DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_dispatch_metadata_project;
ALTER TABLE dispatch_metadata DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_dispatch_pattern_offered_project;
ALTER TABLE dispatch_pattern_offered DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_session_analytics_project;
ALTER TABLE session_analytics DROP COLUMN project_id;

-- ============================================================================
-- @db: runtime_coordination — 6 hot tables
-- NOTE: Apply ONLY to runtime_coordination.db, not quality_intelligence.db
-- ============================================================================

DROP INDEX IF EXISTS idx_dispatches_project;
ALTER TABLE dispatches DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_dispatch_attempts_project;
ALTER TABLE dispatch_attempts DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_terminal_leases_project;
ALTER TABLE terminal_leases DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_coordination_events_project;
ALTER TABLE coordination_events DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_incident_log_project;
ALTER TABLE incident_log DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_intelligence_injections_project;
ALTER TABLE intelligence_injections DROP COLUMN project_id;

-- Remove version stamp
DELETE FROM runtime_schema_version WHERE version = 10;

COMMIT;

PRAGMA foreign_keys = ON;
