-- VNX Migration 0015 — DOWN — cold-table project_id rollback
-- Reverses 0015_complete_project_id.sql: removes project_id column from
-- 11 QI tables and 7 RC tables (the cold-table phase 4 extension).
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- Each DROP COLUMN also removes the associated index (SQLite auto-removes
-- indexes that cover only the dropped column; composite indexes remain).
--
-- Pre-down state: 18 tables have project_id column + per-table index.
-- Post-down state: project_id and its indexes removed from all 18 tables.
--
-- CAUTION: This removes multi-tenant data segregation for cold tables.
-- Only roll back if you are also rolling back 0010 (hot tables). Rolling
-- back 0015 while keeping 0010 leaves the system in a mixed state.
--
-- Applied by: operator (manual) — sqlite3 <db> < this_file.sql
-- Run QI section against quality_intelligence.db
-- Run RC section against runtime_coordination.db

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- @db: quality_intelligence — 11 cold tables
-- ============================================================================

DROP INDEX IF EXISTS idx_vnx_code_quality_project;
ALTER TABLE vnx_code_quality DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_snippet_metadata_project;
ALTER TABLE snippet_metadata DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_quality_trends_project;
ALTER TABLE quality_trends DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_quality_alerts_project;
ALTER TABLE quality_alerts DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_dispatch_quality_context_project;
ALTER TABLE dispatch_quality_context DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_quality_system_metrics_project;
ALTER TABLE quality_system_metrics DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_scan_history_project;
ALTER TABLE scan_history DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_tag_combinations_project;
ALTER TABLE tag_combinations DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_improvement_suggestions_project;
ALTER TABLE improvement_suggestions DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_nightly_digests_project;
ALTER TABLE nightly_digests DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_governance_metrics_project;
ALTER TABLE governance_metrics DROP COLUMN project_id;

-- ============================================================================
-- @db: runtime_coordination — 7 cold tables
-- NOTE: Apply only against runtime_coordination.db, not quality_intelligence.db
-- ============================================================================

DROP INDEX IF EXISTS idx_retry_budgets_project;
ALTER TABLE retry_budgets DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_retry_state_project;
ALTER TABLE retry_state DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_escalation_log_project;
ALTER TABLE escalation_log DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_execution_targets_project;
ALTER TABLE execution_targets DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_inbound_inbox_project;
ALTER TABLE inbound_inbox DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_recommendations_project;
ALTER TABLE recommendations DROP COLUMN project_id;

DROP INDEX IF EXISTS idx_recommendation_outcomes_project;
ALTER TABLE recommendation_outcomes DROP COLUMN project_id;

COMMIT;

PRAGMA foreign_keys = ON;
