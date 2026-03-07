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
-- SESSION ANALYTICS (Conversation Mining)
-- ============================================================================

-- Session-level metrics extracted from Claude Code JSONL logs
CREATE TABLE IF NOT EXISTS session_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    project_path TEXT NOT NULL,
    terminal TEXT,
    session_date DATE NOT NULL,

    -- Token metrics
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,

    -- Tool metrics
    tool_calls_total INTEGER DEFAULT 0,
    tool_read_count INTEGER DEFAULT 0,
    tool_edit_count INTEGER DEFAULT 0,
    tool_bash_count INTEGER DEFAULT 0,
    tool_grep_count INTEGER DEFAULT 0,
    tool_write_count INTEGER DEFAULT 0,
    tool_task_count INTEGER DEFAULT 0,
    tool_other_count INTEGER DEFAULT 0,

    -- Session metrics
    message_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    duration_minutes REAL,

    -- Heuristic flags (phase 2: no LLM needed)
    has_error_recovery BOOLEAN DEFAULT FALSE,
    has_context_reset BOOLEAN DEFAULT FALSE,
    has_large_refactor BOOLEAN DEFAULT FALSE,
    has_test_cycle BOOLEAN DEFAULT FALSE,
    primary_activity TEXT,

    -- LLM deep analysis (phase 3: optional)
    deep_analysis_json TEXT,
    deep_analysis_model TEXT,
    deep_analysis_at DATETIME,

    -- Model identification
    session_model TEXT DEFAULT 'unknown',

    -- Metadata
    file_size_bytes INTEGER,
    analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    analyzer_version TEXT DEFAULT '1.0.0'
);

CREATE INDEX IF NOT EXISTS idx_session_terminal ON session_analytics (terminal, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_session_project ON session_analytics (project_path, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_session_date ON session_analytics (session_date DESC);
CREATE INDEX IF NOT EXISTS idx_session_activity ON session_analytics (primary_activity);
CREATE INDEX IF NOT EXISTS idx_session_model ON session_analytics (session_model, session_date DESC);

-- Improvement suggestions extracted from session analysis
CREATE TABLE IF NOT EXISTS improvement_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    category TEXT NOT NULL,
    component TEXT,
    current_behavior TEXT NOT NULL,
    suggested_improvement TEXT NOT NULL,
    evidence TEXT,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'new',
    digest_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    acted_on_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_improvement_category ON improvement_suggestions (category, status);
CREATE INDEX IF NOT EXISTS idx_improvement_priority ON improvement_suggestions (priority, status);

-- Nightly digest reports
CREATE TABLE IF NOT EXISTS nightly_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date DATE NOT NULL UNIQUE,
    sessions_analyzed INTEGER DEFAULT 0,
    deep_analyzed INTEGER DEFAULT 0,
    new_suggestions INTEGER DEFAULT 0,
    total_tokens_used INTEGER DEFAULT 0,
    digest_markdown TEXT NOT NULL,
    digest_path TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

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

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.0.4-conversation-mining', 'Add session_analytics, improvement_suggestions, nightly_digests tables');

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.0.5-session-model', 'Add session_model column to session_analytics for model-based performance tracking');

-- ============================================================================
-- DISPATCH METADATA (Dispatch Analytics Persistence)
-- ============================================================================

-- Full dispatch lifecycle tracking: dispatch → session → report → receipt
CREATE TABLE IF NOT EXISTS dispatch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL UNIQUE,
    terminal TEXT NOT NULL,
    track TEXT NOT NULL,
    role TEXT,
    skill_name TEXT,
    gate TEXT,
    cognition TEXT DEFAULT 'normal',
    priority TEXT DEFAULT 'P1',
    pr_id TEXT,
    parent_dispatch TEXT,
    pattern_count INTEGER DEFAULT 0,
    prevention_rule_count INTEGER DEFAULT 0,
    intelligence_json TEXT,
    instruction_char_count INTEGER DEFAULT 0,
    context_file_count INTEGER DEFAULT 0,
    dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    outcome_status TEXT,
    outcome_report_path TEXT,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatch_meta_id ON dispatch_metadata (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_meta_terminal ON dispatch_metadata (terminal);
CREATE INDEX IF NOT EXISTS idx_dispatch_meta_role ON dispatch_metadata (role);
CREATE INDEX IF NOT EXISTS idx_dispatch_meta_gate ON dispatch_metadata (gate);
CREATE INDEX IF NOT EXISTS idx_dispatch_meta_outcome ON dispatch_metadata (outcome_status);
CREATE INDEX IF NOT EXISTS idx_dispatch_meta_dispatched ON dispatch_metadata (dispatched_at DESC);

-- Analytics views for dispatch correlation

CREATE VIEW IF NOT EXISTS dispatch_success_by_role AS
SELECT
    role,
    COUNT(*) as total_dispatches,
    SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
    ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
    AVG(pattern_count) as avg_patterns,
    AVG(prevention_rule_count) as avg_rules,
    AVG(instruction_char_count) as avg_instruction_chars
FROM dispatch_metadata
WHERE outcome_status IS NOT NULL
GROUP BY role
ORDER BY total_dispatches DESC;

CREATE VIEW IF NOT EXISTS intelligence_effectiveness AS
SELECT
    CASE WHEN intelligence_json IS NOT NULL AND intelligence_json != '' THEN 'with_intelligence' ELSE 'without_intelligence' END as intelligence_used,
    COUNT(*) as total,
    SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
    ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
    AVG(pattern_count) as avg_patterns
FROM dispatch_metadata
WHERE outcome_status IS NOT NULL
GROUP BY intelligence_used;

CREATE VIEW IF NOT EXISTS cost_per_dispatch AS
SELECT
    dm.dispatch_id,
    dm.terminal,
    dm.role,
    dm.gate,
    dm.outcome_status,
    sa.session_model,
    sa.total_input_tokens,
    sa.total_output_tokens,
    sa.tool_calls_total,
    sa.duration_minutes,
    dm.pattern_count,
    dm.instruction_char_count
FROM dispatch_metadata dm
LEFT JOIN session_analytics sa ON sa.dispatch_id = dm.dispatch_id
WHERE dm.outcome_status IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.0.6-dispatch-analytics', 'Add dispatch_metadata table, dispatch_id columns, and analytics views');

-- ============================================================================
-- GOVERNANCE MEASUREMENT (Objective Quality Scoring + SPC)
-- ============================================================================

-- Governance metrics aggregated nightly per scope
CREATE TABLE IF NOT EXISTS governance_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    scope_type TEXT NOT NULL,     -- 'system'|'terminal'|'role'|'gate'|'model'
    scope_value TEXT NOT NULL,
    metric_name TEXT NOT NULL,    -- 'fpy'|'rework_rate'|'gate_velocity_hours'|'mean_cqs'|'dispatch_count'
    metric_value REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gov_metrics_lookup
    ON governance_metrics (period_start, scope_type, metric_name);

-- SPC control limits (X-bar +/- 3 sigma)
CREATE TABLE IF NOT EXISTS spc_control_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    center_line REAL NOT NULL,    -- X-bar (mean)
    ucl REAL NOT NULL,            -- Upper Control Limit (X-bar + 3 sigma)
    lcl REAL NOT NULL,            -- Lower Control Limit (X-bar - 3 sigma)
    sigma REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    baseline_start DATE,
    baseline_end DATE,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(metric_name, scope_type, scope_value)
);

-- SPC anomaly alerts
CREATE TABLE IF NOT EXISTS spc_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,     -- 'out_of_control'|'trend'|'shift'|'run'
    metric_name TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    observed_value REAL NOT NULL,
    control_limit REAL,
    description TEXT,
    severity TEXT DEFAULT 'warning', -- 'info'|'warning'|'critical'
    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    acknowledged_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_spc_alerts_lookup
    ON spc_alerts (detected_at DESC, severity);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.1.0-governance', 'Add governance_metrics, spc_control_limits, spc_alerts tables and CQS columns on dispatch_metadata');

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.2.0-cqs-advisory-oi', 'Add target_open_items, open_items_created, open_items_resolved columns to dispatch_metadata for T0 advisory and OI delta CQS components');
