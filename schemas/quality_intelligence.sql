-- VNX Quality Intelligence Database Schema
-- Version: 8.0.2 (Phase 2)
-- Purpose: Track code quality metrics, patterns, and professional code snippets
-- Database: SQLite with FTS5 for full-text search

-- ============================================================================
-- CORE QUALITY METRICS
-- ============================================================================

-- File-level quality metrics
CREATE TABLE IF NOT EXISTS vnx_code_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    project_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,

    -- Size metrics
    line_count INTEGER DEFAULT 0,
    code_lines INTEGER DEFAULT 0,
    comment_lines INTEGER DEFAULT 0,
    blank_lines INTEGER DEFAULT 0,

    -- Complexity metrics
    complexity_score REAL DEFAULT 0.0,
    cyclomatic_complexity INTEGER DEFAULT 0,
    cognitive_complexity INTEGER DEFAULT 0,

    -- Structure metrics
    function_count INTEGER DEFAULT 0,
    class_count INTEGER DEFAULT 0,
    import_count INTEGER DEFAULT 0,
    max_function_length INTEGER DEFAULT 0,
    max_nesting_depth INTEGER DEFAULT 0,

    -- Quality indicators
    has_tests BOOLEAN DEFAULT FALSE,
    test_coverage REAL DEFAULT 0.0,
    has_docstrings BOOLEAN DEFAULT FALSE,
    docstring_coverage REAL DEFAULT 0.0,

    -- Issue tracking
    quality_warnings TEXT, -- JSON array of warnings
    critical_issues INTEGER DEFAULT 0,
    warning_issues INTEGER DEFAULT 0,
    info_issues INTEGER DEFAULT 0,

    -- Track assignment (for context routing)
    suggested_track TEXT, -- A (storage), B (refactor), C (investigation), null
    track_confidence REAL DEFAULT 0.0,

    -- Metadata
    language TEXT,
    framework TEXT,
    last_scan DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_modified DATETIME,
    scan_version TEXT DEFAULT '1.0'
);

-- Indexes for vnx_code_quality table
CREATE INDEX IF NOT EXISTS idx_quality_track ON vnx_code_quality (suggested_track);
CREATE INDEX IF NOT EXISTS idx_quality_warnings ON vnx_code_quality (critical_issues DESC);
CREATE INDEX IF NOT EXISTS idx_quality_complexity ON vnx_code_quality (complexity_score DESC);
CREATE INDEX IF NOT EXISTS idx_quality_scan ON vnx_code_quality (last_scan DESC);

-- ============================================================================
-- CODE SNIPPET MANAGEMENT (FTS5 for full-text search)
-- ============================================================================

-- Professional code snippets with semantic search
CREATE VIRTUAL TABLE IF NOT EXISTS code_snippets USING fts5(
    title,              -- Function/class/pattern name
    description,        -- Brief description of what it does
    code,              -- Actual code snippet
    file_path,         -- Source file location
    line_range,        -- "start-end" line numbers
    tags,              -- Categories: crawler, storage, extraction, etc.
    language,          -- python, bash, sql, etc.
    framework,         -- crawl4ai, supabase, etc.
    dependencies,      -- Required imports/packages
    quality_score,     -- 0-100 quality assessment
    usage_count,       -- How many times referenced
    last_updated,      -- Timestamp of last update
    tokenize = 'porter unicode61'
);

-- Snippet metadata (for non-FTS queries)
CREATE TABLE IF NOT EXISTS snippet_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snippet_rowid INTEGER NOT NULL, -- Reference to code_snippets rowid
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    quality_score REAL DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0,
    source_commit_hash TEXT,        -- Git commit hash at extraction time
    pattern_hash TEXT,              -- SHA1(title|file_path|line_range) for O(1) usage lookup
    extracted_at DATETIME,          -- When snippet was extracted from source
    verified_at DATETIME,           -- Last staleness verification timestamp
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for snippet_metadata table
CREATE INDEX IF NOT EXISTS idx_snippet_quality ON snippet_metadata (quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_snippet_usage ON snippet_metadata (usage_count DESC);
CREATE INDEX IF NOT EXISTS idx_snippet_file ON snippet_metadata (file_path);
CREATE INDEX IF NOT EXISTS idx_snippet_pattern_hash ON snippet_metadata (pattern_hash);

-- ============================================================================
-- QUALITY TRENDS & ANALYTICS
-- ============================================================================

-- Track quality metrics over time for trend analysis
CREATE TABLE IF NOT EXISTS quality_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for quality_trends table
CREATE INDEX IF NOT EXISTS idx_trends_file ON quality_trends (file_path, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trends_metric ON quality_trends (metric_name, timestamp DESC);

-- Quality alerts and recommendations
CREATE TABLE IF NOT EXISTS quality_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    alert_type TEXT NOT NULL, -- warning, error, improvement, refactor
    severity TEXT NOT NULL, -- critical, high, medium, low
    category TEXT, -- complexity, duplication, security, performance
    message TEXT NOT NULL,
    suggested_action TEXT,
    context_snippet TEXT, -- Code context for the alert

    -- Status tracking
    status TEXT DEFAULT 'open', -- open, acknowledged, resolved, ignored
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    acknowledged_at DATETIME,
    resolved_at DATETIME,
    resolved_by TEXT, -- Which terminal resolved it

    -- Linking
    related_dispatch_id TEXT,
    related_receipt_id TEXT
);

-- Indexes for quality_alerts table
CREATE INDEX IF NOT EXISTS idx_alerts_status ON quality_alerts (status, severity DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_file ON quality_alerts (file_path, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON quality_alerts (alert_type, status);

-- ============================================================================
-- PATTERN RECOGNITION & LEARNING
-- ============================================================================

-- Success patterns extracted from completed tasks
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL, -- approach, solution, architecture
    category TEXT NOT NULL, -- crawler, storage, extraction, etc.
    title TEXT NOT NULL,
    description TEXT NOT NULL,

    -- Pattern data
    pattern_data TEXT NOT NULL, -- JSON with detailed pattern info
    code_example TEXT,
    prerequisites TEXT, -- JSON array
    outcomes TEXT, -- JSON array of expected outcomes

    -- Metrics
    success_rate REAL DEFAULT 0.0, -- 0-1.0
    usage_count INTEGER DEFAULT 0,
    avg_completion_time INTEGER, -- seconds
    confidence_score REAL DEFAULT 0.0, -- 0-1.0

    -- Source tracking
    source_dispatch_ids TEXT, -- JSON array of dispatch IDs
    source_receipts TEXT, -- JSON array of receipt data
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME
);

-- Indexes for success_patterns table
CREATE INDEX IF NOT EXISTS idx_patterns_category ON success_patterns (category, success_rate DESC);
CREATE INDEX IF NOT EXISTS idx_patterns_usage ON success_patterns (usage_count DESC);

-- Anti-patterns to avoid
CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL, -- approach, implementation, architecture
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,

    -- Anti-pattern data
    pattern_data TEXT NOT NULL, -- JSON with detailed pattern info
    problem_example TEXT,
    why_problematic TEXT NOT NULL,
    better_alternative TEXT,

    -- Metrics
    occurrence_count INTEGER DEFAULT 0,
    avg_resolution_time INTEGER, -- seconds
    severity TEXT DEFAULT 'medium', -- critical, high, medium, low

    -- Source tracking
    source_dispatch_ids TEXT, -- JSON array
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME
);

-- Indexes for antipatterns table
CREATE INDEX IF NOT EXISTS idx_antipatterns_severity ON antipatterns (severity, occurrence_count DESC);
CREATE INDEX IF NOT EXISTS idx_antipatterns_category ON antipatterns (category);

-- ============================================================================
-- DISPATCH INTELLIGENCE INTEGRATION
-- ============================================================================

-- Link quality metrics to dispatch decisions
CREATE TABLE IF NOT EXISTS dispatch_quality_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL UNIQUE,

    -- Quality context provided
    files_analyzed INTEGER DEFAULT 0,
    quality_warnings_flagged INTEGER DEFAULT 0,
    patterns_suggested INTEGER DEFAULT 0,
    snippets_provided INTEGER DEFAULT 0,

    -- Context quality
    context_quality_score REAL DEFAULT 0.0, -- 0-100
    context_token_count INTEGER DEFAULT 0,

    -- Outcome tracking
    task_completed BOOLEAN DEFAULT FALSE,
    task_success BOOLEAN DEFAULT FALSE,
    completion_time INTEGER, -- seconds
    context_effectiveness REAL, -- 0-1.0 (was context helpful?)

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME
);

-- Indexes for dispatch_quality_context table
CREATE INDEX IF NOT EXISTS idx_dispatch_quality ON dispatch_quality_context (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_effectiveness ON dispatch_quality_context (context_effectiveness DESC);

-- ============================================================================
-- SYSTEM HEALTH & MONITORING
-- ============================================================================

-- Track quality system health and performance
CREATE TABLE IF NOT EXISTS quality_system_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_unit TEXT, -- seconds, count, percentage, etc.
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for quality_system_metrics table
CREATE INDEX IF NOT EXISTS idx_system_metrics ON quality_system_metrics (metric_name, timestamp DESC);

-- Quality scan history
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type TEXT NOT NULL, -- full, incremental, targeted
    files_scanned INTEGER DEFAULT 0,
    files_changed INTEGER DEFAULT 0,
    issues_found INTEGER DEFAULT 0,
    scan_duration_seconds REAL,

    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    status TEXT DEFAULT 'running', -- running, completed, failed
    error_message TEXT
);

-- Indexes for scan_history table
CREATE INDEX IF NOT EXISTS idx_scan_history ON scan_history (started_at DESC);

-- ============================================================================
-- PATTERN USAGE TRACKING (Feedback Loop)
-- ============================================================================

-- Track which patterns are offered and used by terminals
CREATE TABLE IF NOT EXISTS pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    last_offered TIMESTAMP,
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pattern_usage_hash ON pattern_usage (pattern_hash);
CREATE INDEX IF NOT EXISTS idx_pattern_usage_confidence ON pattern_usage (confidence DESC);

-- ============================================================================
-- TAG INTELLIGENCE
-- ============================================================================

-- Track tag combination occurrences for prevention rule generation
CREATE TABLE IF NOT EXISTS tag_combinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_tuple TEXT NOT NULL UNIQUE,
    occurrence_count INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    phases TEXT,
    terminals TEXT,
    outcomes TEXT
);

CREATE INDEX IF NOT EXISTS idx_tag_tuple ON tag_combinations (tag_tuple);

-- Prevention rules generated from recurring tag patterns
CREATE TABLE IF NOT EXISTS prevention_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_combination TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    description TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    triggered_count INTEGER DEFAULT 0,
    last_triggered TEXT
);

CREATE INDEX IF NOT EXISTS idx_rule_combination ON prevention_rules (tag_combination);
CREATE INDEX IF NOT EXISTS idx_rule_confidence ON prevention_rules (confidence DESC);

-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- High-quality code snippets (score >= 80)
CREATE VIEW IF NOT EXISTS high_quality_snippets AS
SELECT
    s.rowid,
    s.title,
    s.description,
    s.file_path,
    s.tags,
    s.language,
    m.quality_score,
    m.usage_count
FROM code_snippets s
JOIN snippet_metadata m ON s.rowid = m.snippet_rowid
WHERE m.quality_score >= 80
ORDER BY m.quality_score DESC, m.usage_count DESC;

-- Files needing attention (critical issues or high complexity)
CREATE VIEW IF NOT EXISTS files_needing_attention AS
SELECT
    file_path,
    complexity_score,
    critical_issues,
    warning_issues,
    suggested_track,
    last_scan
FROM vnx_code_quality
WHERE critical_issues > 0
   OR complexity_score > 75
   OR (line_count > 500 AND function_count = 0)
ORDER BY critical_issues DESC, complexity_score DESC;

-- Open quality alerts by severity
CREATE VIEW IF NOT EXISTS open_alerts_summary AS
SELECT
    severity,
    alert_type,
    COUNT(*) as alert_count,
    MIN(created_at) as oldest_alert
FROM quality_alerts
WHERE status = 'open'
GROUP BY severity, alert_type
ORDER BY
    CASE severity
        WHEN 'critical' THEN 1
        WHEN 'high' THEN 2
        WHEN 'medium' THEN 3
        WHEN 'low' THEN 4
    END,
    alert_count DESC;

-- ============================================================================
-- INITIALIZATION METADATA
-- ============================================================================

-- Store schema version and migration history
CREATE TABLE IF NOT EXISTS schema_version (
    version TEXT PRIMARY KEY,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.0.2-phase2', 'Initial Quality Intelligence Database schema');

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.0.3-intelligence-db', 'Add pattern_usage, tag_combinations, prevention_rules; citation fields in snippet_metadata');
