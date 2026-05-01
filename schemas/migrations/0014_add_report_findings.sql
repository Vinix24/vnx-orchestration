-- Migration 0014: Add report_findings table
--
-- Background (OI-1155):
--   The Phase-3 session-dispatch-link step in nightly_intelligence_pipeline.sh
--   queries report_findings before the table is guaranteed to exist. If Phase 0
--   (quality_db_init.py) fails or was skipped, Phase 3 raises:
--     sqlite3.OperationalError: no such table: report_findings
--
-- This migration is idempotent via CREATE TABLE IF NOT EXISTS and
-- CREATE INDEX IF NOT EXISTS. Applying it twice is safe.
--
-- Columns derived from all consumer SELECT/INSERT statements:
--   link_sessions_dispatches.py   — id, report_path, dispatch_id
--   intelligence_queries.py       — report_path, task_type, summary, tags_found,
--                                    patterns_found, antipatterns_found, report_date,
--                                    terminal, extracted_at
--   cached_intelligence.py        — report_path, task_type, summary, tags_found,
--                                    patterns_found, antipatterns_found, report_date
--   gather_intelligence.py        — report_path, task_type, summary, tags_found,
--                                    patterns_found, antipatterns_found,
--                                    prevention_rules_found, report_date, terminal,
--                                    extracted_at

CREATE TABLE IF NOT EXISTS report_findings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    report_path             TEXT    NOT NULL,
    report_date             TIMESTAMP,
    terminal                TEXT,
    task_type               TEXT,
    patterns_found          INTEGER,
    antipatterns_found      INTEGER,
    prevention_rules_found  INTEGER,
    tags_found              TEXT,
    summary                 TEXT,
    age_category            TEXT,
    extracted_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    dispatch_id             TEXT
);

CREATE INDEX IF NOT EXISTS idx_report_findings_extracted
    ON report_findings (extracted_at DESC);

CREATE INDEX IF NOT EXISTS idx_report_findings_dispatch
    ON report_findings (dispatch_id);
