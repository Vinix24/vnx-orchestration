-- VNX Migration 0015 — Phase 6 P4 of single-VNX consolidation
-- (Numbered 0015 because 0011-0014 were claimed by intervening migrations
--  in PRs #391-#418; the FEATURE_PLAN's "0011_complete_project_id.sql"
--  filename refers to logical Phase 4 ordering, not on-disk sequence.)
--
-- Extends Phase 0's project_id columns from the hot tables (migration
-- 0010) to the remaining 18 tables across `quality_intelligence.db` and
-- `runtime_coordination.db`. Additive-only: every ALTER includes
-- `DEFAULT 'vnx-dev'` so existing INSERTs continue to land without code
-- changes. The Python runner (`scripts/lib/project_id_migration_p4.py`)
-- applies these statements idempotently and skips tables that do not
-- exist in a given DB.
--
-- Companion plan: `claudedocs/2026-04-30-single-vnx-migration-plan.md`
-- (§4.1, §6 Phase 4) and `roadmap/features/phase-06-single-system-migration/FEATURE_PLAN.md`
-- §w6-p4 Risk-Mitigation Steps.
--
-- Verification (after apply):
--   sqlite3 ~/.vnx-data/state/quality_intelligence.db \
--     "SELECT name FROM pragma_table_info('vnx_code_quality') WHERE name='project_id';"
--   -> single row: project_id

-- ============================================================================
-- @db: quality_intelligence (Phase 4 cold tables — 11 tables)
-- ============================================================================

ALTER TABLE vnx_code_quality          ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE snippet_metadata          ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE quality_trends            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE quality_alerts            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE dispatch_quality_context  ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE quality_system_metrics    ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE scan_history              ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE tag_combinations          ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE improvement_suggestions   ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE nightly_digests           ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE governance_metrics        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';

CREATE INDEX IF NOT EXISTS idx_vnx_code_quality_project         ON vnx_code_quality(project_id);
CREATE INDEX IF NOT EXISTS idx_snippet_metadata_project         ON snippet_metadata(project_id);
CREATE INDEX IF NOT EXISTS idx_quality_trends_project           ON quality_trends(project_id);
CREATE INDEX IF NOT EXISTS idx_quality_alerts_project           ON quality_alerts(project_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_quality_context_project ON dispatch_quality_context(project_id);
CREATE INDEX IF NOT EXISTS idx_quality_system_metrics_project   ON quality_system_metrics(project_id);
CREATE INDEX IF NOT EXISTS idx_scan_history_project             ON scan_history(project_id);
CREATE INDEX IF NOT EXISTS idx_tag_combinations_project         ON tag_combinations(project_id);
CREATE INDEX IF NOT EXISTS idx_improvement_suggestions_project  ON improvement_suggestions(project_id);
CREATE INDEX IF NOT EXISTS idx_nightly_digests_project          ON nightly_digests(project_id);
CREATE INDEX IF NOT EXISTS idx_governance_metrics_project       ON governance_metrics(project_id);

-- ============================================================================
-- @db: runtime_coordination (Phase 4 cold tables — 7 tables)
-- ============================================================================

ALTER TABLE retry_budgets             ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE retry_state               ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE escalation_log            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE execution_targets         ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE inbound_inbox             ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE recommendations           ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
ALTER TABLE recommendation_outcomes   ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';

CREATE INDEX IF NOT EXISTS idx_retry_budgets_project           ON retry_budgets(project_id);
CREATE INDEX IF NOT EXISTS idx_retry_state_project             ON retry_state(project_id);
CREATE INDEX IF NOT EXISTS idx_escalation_log_project          ON escalation_log(project_id);
CREATE INDEX IF NOT EXISTS idx_execution_targets_project       ON execution_targets(project_id);
CREATE INDEX IF NOT EXISTS idx_inbound_inbox_project           ON inbound_inbox(project_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_project         ON recommendations(project_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_project ON recommendation_outcomes(project_id);

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (11, 'Phase 6 P4 single-VNX migration: extend project_id to remaining 18 tables (quality_intelligence + runtime_coordination)');
