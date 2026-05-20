-- SEOcrawler-v2 pre-central-migration schema prep
-- Purpose: close 26 schema-drift issues detected in Wave 2a dry-run before
--          migrate_to_central_vnx.py --apply can run against seocrawler-v2.
--
-- Execution model: do NOT run this file directly against the live DB.
--   Use scripts/apply_schema_prep.py which applies each statement
--   idempotently (PRAGMA table_info check before every ALTER TABLE, and
--   CREATE TABLE IF NOT EXISTS for missing tables).
--
-- Reference: claudedocs/wave2a-dag1-dry-run-2026-05-20.md §seocrawler-v2
-- Drift count resolved: 26

-- ============================================================================
-- quality_intelligence.db — missing project_id columns (same 8 as sales-copilot)
-- ============================================================================

ALTER TABLE prevention_rules        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE dispatch_pattern_offered ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE session_analytics       ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE confidence_events       ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE success_patterns        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE antipatterns            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE dispatch_metadata       ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE pattern_usage           ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';

-- ============================================================================
-- runtime_coordination.db — missing project_id columns (same 6 as sales-copilot)
-- ============================================================================

ALTER TABLE dispatch_attempts       ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE incident_log            ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE dispatches              ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE terminal_leases         ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE coordination_events     ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';
ALTER TABLE intelligence_injections ADD COLUMN project_id TEXT NOT NULL DEFAULT 'seocrawler-v2';

-- ============================================================================
-- quality_intelligence.db — 2 missing tables (seocrawler-v2 specific)
-- ============================================================================

-- confidence_events (missing entirely in seocrawler-v2)
CREATE TABLE IF NOT EXISTS confidence_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id         TEXT NOT NULL,
    project_id          TEXT NOT NULL DEFAULT 'seocrawler-v2',
    terminal            TEXT,
    outcome             TEXT NOT NULL,
    patterns_boosted    INTEGER DEFAULT 0,
    patterns_decayed    INTEGER DEFAULT 0,
    confidence_change   REAL NOT NULL,
    occurred_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conf_events_dispatch  ON confidence_events (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_conf_events_occurred  ON confidence_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_conf_events_project   ON confidence_events (project_id);

-- dispatch_pattern_offered (missing entirely in seocrawler-v2)
CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
    dispatch_id     TEXT NOT NULL,
    pattern_id      TEXT NOT NULL,
    pattern_title   TEXT NOT NULL,
    offered_at      TEXT NOT NULL,
    project_id      TEXT NOT NULL DEFAULT 'seocrawler-v2',
    PRIMARY KEY (dispatch_id, pattern_id)
);

CREATE INDEX IF NOT EXISTS idx_dpo_dispatch_id  ON dispatch_pattern_offered (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_dpo_project      ON dispatch_pattern_offered (project_id);

-- ============================================================================
-- quality_intelligence.db — prevention_rules additional columns
-- (4 columns missing: valid_from, valid_until, source, source_dispatch_id)
-- ============================================================================

ALTER TABLE prevention_rules ADD COLUMN valid_from        TEXT;
ALTER TABLE prevention_rules ADD COLUMN valid_until       TEXT;
ALTER TABLE prevention_rules ADD COLUMN source            TEXT;
ALTER TABLE prevention_rules ADD COLUMN source_dispatch_id TEXT;

-- ============================================================================
-- quality_intelligence.db — session_analytics additional columns
-- (2 columns missing: context_reset_count, quality_advisory_json)
-- ============================================================================

ALTER TABLE session_analytics ADD COLUMN context_reset_count   INTEGER DEFAULT 0;
ALTER TABLE session_analytics ADD COLUMN quality_advisory_json TEXT;

-- ============================================================================
-- quality_intelligence.db — dispatch_metadata additional columns
-- (2 columns missing: context_reset_count, quality_advisory_json)
-- ============================================================================

ALTER TABLE dispatch_metadata ADD COLUMN context_reset_count   INTEGER DEFAULT 0;
ALTER TABLE dispatch_metadata ADD COLUMN quality_advisory_json TEXT;

-- ============================================================================
-- dispatch_tracker.db — missing dispatch_experiments table
-- ============================================================================

CREATE TABLE IF NOT EXISTS dispatch_experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id         TEXT UNIQUE,
    project_id          TEXT NOT NULL DEFAULT 'seocrawler-v2',
    timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
    instruction_chars   INTEGER,
    context_items       INTEGER,
    repo_map_symbols    INTEGER,
    role                TEXT,
    cognition           TEXT,
    model               TEXT,
    terminal            TEXT,
    file_count          INTEGER,
    success             BOOLEAN,
    cqs                 REAL,
    completion_minutes  REAL,
    test_count          INTEGER,
    committed           BOOLEAN,
    lines_changed       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_de_dispatch_id  ON dispatch_experiments (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_de_role         ON dispatch_experiments (role);
CREATE INDEX IF NOT EXISTS idx_de_timestamp    ON dispatch_experiments (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_de_project_id   ON dispatch_experiments (project_id);
