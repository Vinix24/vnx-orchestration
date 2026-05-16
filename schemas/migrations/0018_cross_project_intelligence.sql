-- Migration 0018: cross-project intelligence (global_intelligence.db schema)
-- Wave 5 PR-5.4
--
-- This SQL describes the schema for global_intelligence.db, which is a
-- SEPARATE database from per-project quality_intelligence.db files.
-- Per-project databases are never modified by the aggregator (read-only).
--
-- The global_intelligence.db is created on-demand by IntelligenceAggregator
-- at an operator-chosen path (e.g. ~/.vnx-aggregator/global_intelligence.db).

-- Global patterns: normalized, privacy-safe summaries mined across N projects.
-- Only family-keys (path-stripped, ID-removed) and occurrence counts are stored.
-- No project-specific file paths, dispatch IDs, or raw description text are
-- permitted in this table.
CREATE TABLE IF NOT EXISTS global_patterns (
    pattern_id   TEXT PRIMARY KEY,           -- SHA1(family_key), prefixed "gp-"
    pattern_family TEXT NOT NULL,            -- normalized family key (no paths/IDs)
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    occurrences_per_project TEXT,            -- JSON {project_id: count}
    avg_confidence REAL,
    first_seen TEXT,
    last_seen TEXT
);

CREATE INDEX IF NOT EXISTS idx_global_patterns_family
    ON global_patterns(pattern_family);

-- Cross-project recommendations: one row per (source, target, pattern) triple.
-- consumed_at is set by the selector when the recommendation has been injected
-- into a dispatch.
CREATE TABLE IF NOT EXISTS cross_project_recommendations (
    rec_id TEXT PRIMARY KEY,                 -- UUID
    source_project TEXT NOT NULL,
    target_project TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    rationale TEXT,
    confidence REAL,
    created_at TEXT,
    consumed_at TEXT,
    FOREIGN KEY (pattern_id) REFERENCES global_patterns(pattern_id)
);

CREATE INDEX IF NOT EXISTS idx_xprec_target
    ON cross_project_recommendations(target_project, consumed_at);

CREATE INDEX IF NOT EXISTS idx_xprec_source
    ON cross_project_recommendations(source_project);
