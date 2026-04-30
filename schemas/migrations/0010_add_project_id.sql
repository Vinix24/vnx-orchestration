-- VNX Migration 0010 — Phase 0 of single-VNX consolidation
-- Adds project_id TEXT NOT NULL DEFAULT 'vnx-dev' to hot tables in
-- quality_intelligence.db and runtime_coordination.db.
--
-- ADDITIVE-ONLY change: existing call sites continue to work unchanged.
-- The DEFAULT means every existing INSERT lands as 'vnx-dev'; readers that
-- ignore the new column see all rows. No behavior change.
--
-- Companion plan: claudedocs/2026-04-30-single-vnx-migration-plan.md (§4.1, §6 Phase 0).
--
-- This file is the canonical reference for the migration. The Python runner
-- in scripts/runtime_coordination_init.py applies it idempotently per DB,
-- skipping tables that do not exist in a given DB and skipping ALTERs whose
-- column is already present.
--
-- Verification (after apply):
--   sqlite3 .vnx-data/state/quality_intelligence.db \
--     "SELECT project_id, COUNT(*) FROM success_patterns GROUP BY project_id;"
--   -> single row: vnx-dev, N

-- ============================================================================
-- @db: quality_intelligence
-- ============================================================================

ALTER TABLE success_patterns         ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE antipatterns             ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE prevention_rules         ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE pattern_usage            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE confidence_events        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE dispatch_metadata        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE dispatch_pattern_offered ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE session_analytics        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';

CREATE INDEX IF NOT EXISTS idx_success_patterns_project         ON success_patterns(project_id);
CREATE INDEX IF NOT EXISTS idx_antipatterns_project             ON antipatterns(project_id);
CREATE INDEX IF NOT EXISTS idx_prevention_rules_project         ON prevention_rules(project_id);
CREATE INDEX IF NOT EXISTS idx_pattern_usage_project            ON pattern_usage(project_id);
CREATE INDEX IF NOT EXISTS idx_confidence_events_project        ON confidence_events(project_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_metadata_project        ON dispatch_metadata(project_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_pattern_offered_project ON dispatch_pattern_offered(project_id);
CREATE INDEX IF NOT EXISTS idx_session_analytics_project        ON session_analytics(project_id);

-- ============================================================================
-- @db: runtime_coordination
-- ============================================================================

ALTER TABLE dispatches               ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE dispatch_attempts        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE terminal_leases          ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE coordination_events      ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE incident_log             ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE intelligence_injections  ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';

CREATE INDEX IF NOT EXISTS idx_dispatches_project              ON dispatches(project_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_project       ON dispatch_attempts(project_id);
CREATE INDEX IF NOT EXISTS idx_terminal_leases_project         ON terminal_leases(project_id);
CREATE INDEX IF NOT EXISTS idx_coordination_events_project     ON coordination_events(project_id);
CREATE INDEX IF NOT EXISTS idx_incident_log_project            ON incident_log(project_id);
CREATE INDEX IF NOT EXISTS idx_intelligence_injections_project ON intelligence_injections(project_id);

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (10, 'Phase 0 single-VNX migration: add project_id column + indexes to hot tables');
