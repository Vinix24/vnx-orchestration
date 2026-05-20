-- Sales-copilot pre-central-migration schema prep
-- Purpose: close 16 schema-drift issues detected in Wave 2a dry-run before
--          migrate_to_central_vnx.py --apply can run against sales-copilot.
--
-- Execution model: do NOT run this file directly against the live DB.
--   Use scripts/apply_schema_prep.py which applies each statement
--   idempotently (PRAGMA table_info check before every ALTER TABLE).
--
-- Reference: claudedocs/wave2a-dag1-dry-run-2026-05-20.md §sales-copilot
-- Drift count resolved: 16

-- ============================================================================
-- quality_intelligence.db — 8 missing project_id columns
-- ============================================================================

-- prevention_rules
ALTER TABLE prevention_rules ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- dispatch_pattern_offered
ALTER TABLE dispatch_pattern_offered ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- session_analytics
ALTER TABLE session_analytics ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- confidence_events
ALTER TABLE confidence_events ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- success_patterns
ALTER TABLE success_patterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- antipatterns
ALTER TABLE antipatterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- dispatch_metadata
ALTER TABLE dispatch_metadata ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- pattern_usage
ALTER TABLE pattern_usage ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- ============================================================================
-- runtime_coordination.db — 6 missing project_id columns
-- ============================================================================

-- dispatch_attempts
ALTER TABLE dispatch_attempts ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- incident_log (if table exists — some installs omit this)
ALTER TABLE incident_log ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- dispatches
ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- terminal_leases
ALTER TABLE terminal_leases ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- coordination_events
ALTER TABLE coordination_events ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- intelligence_injections
ALTER TABLE intelligence_injections ADD COLUMN project_id TEXT NOT NULL DEFAULT 'sales-copilot';

-- ============================================================================
-- dispatch_tracker.db — missing dispatch_experiments table (1 issue)
-- ============================================================================

CREATE TABLE IF NOT EXISTS dispatch_experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id         TEXT UNIQUE,
    project_id          TEXT NOT NULL DEFAULT 'sales-copilot',
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
