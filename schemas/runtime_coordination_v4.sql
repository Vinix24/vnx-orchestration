-- VNX Runtime Coordination Schema — Migration v4 (FP-C PR-0)
-- Purpose: Execution targets, task class metadata, intelligence injection,
--          and recommendation tracking for mixed execution routing.
-- Applies on top of: v3 (PR-2) — retry_state and escalation_log already exist
--
-- Design notes:
--   - execution_targets registers all target types (interactive, headless, channel adapter)
--   - dispatches table gains task_class, target_type, target_id, channel_origin, intelligence_payload
--   - intelligence_injections tracks every injection decision for audit
--   - recommendations and recommendation_outcomes track the advisory loop
--   - All tables use CREATE TABLE IF NOT EXISTS for idempotency
--
-- Governance:
--   G-R1: Execution target selection is explicit (execution_targets table)
--   G-R3: Headless execution produces receipts (same dispatch_attempts flow)
--   G-R5: Intelligence injection bounded to 3 items (enforced in code, audited here)
--   G-R6: Evidence metadata required (intelligence_injections.items_json schema)
--   G-R7: Recommendation adoption measured (recommendation_outcomes table)
--   G-R8: All routing decisions emit coordination_events

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- EXECUTION TARGETS
-- ============================================================================
-- Registry of all execution targets. One row per target.
-- Interactive tmux targets map 1:1 to terminals (T1/T2/T3).
-- Headless CLI targets may share a terminal_id or be standalone.
-- Channel adapters have terminal_id = NULL.
--
-- Target types:
--   interactive_tmux_claude | interactive_tmux_codex
--   headless_claude_cli | headless_codex_cli
--   channel_adapter
--
-- Health states:
--   healthy | degraded | unhealthy | offline

CREATE TABLE IF NOT EXISTS execution_targets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id           TEXT    NOT NULL UNIQUE,
    target_type         TEXT    NOT NULL,
    terminal_id         TEXT,
    capabilities_json   TEXT    NOT NULL DEFAULT '[]',
    health              TEXT    NOT NULL DEFAULT 'offline',
    health_checked_at   TEXT,
    model               TEXT,
    registered_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json       TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_target_type
    ON execution_targets (target_type);
CREATE INDEX IF NOT EXISTS idx_target_terminal
    ON execution_targets (terminal_id);
CREATE INDEX IF NOT EXISTS idx_target_health
    ON execution_targets (health);

-- ============================================================================
-- DISPATCHES TABLE EXTENSIONS
-- ============================================================================
-- Add FP-C columns to the existing dispatches table.
-- These columns are nullable for backward compatibility with pre-FPC dispatches.

-- task_class: canonical task class from the FP-C contract
-- Allowed values: coding_interactive, research_structured, docs_synthesis,
--                 ops_watchdog, channel_response
ALTER TABLE dispatches ADD COLUMN task_class TEXT;

-- target_type: execution target type selected by router or overridden by T0
ALTER TABLE dispatches ADD COLUMN target_type TEXT;

-- target_id: specific execution target ID (router-selected or T0-override)
ALTER TABLE dispatches ADD COLUMN target_id TEXT;

-- channel_origin: channel identifier if dispatch originated from inbound event
ALTER TABLE dispatches ADD COLUMN channel_origin TEXT;

-- intelligence_payload: JSON blob containing bounded intelligence items
ALTER TABLE dispatches ADD COLUMN intelligence_payload TEXT;

CREATE INDEX IF NOT EXISTS idx_dispatch_task_class
    ON dispatches (task_class);
CREATE INDEX IF NOT EXISTS idx_dispatch_target_type
    ON dispatches (target_type);
CREATE INDEX IF NOT EXISTS idx_dispatch_channel
    ON dispatches (channel_origin);

-- ============================================================================
-- INTELLIGENCE INJECTIONS (audit trail)
-- ============================================================================
-- One row per injection decision (including suppressions).
-- Links to dispatches via dispatch_id.

CREATE TABLE IF NOT EXISTS intelligence_injections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    injection_id        TEXT    NOT NULL UNIQUE,
    dispatch_id         TEXT    NOT NULL,
    injection_point     TEXT    NOT NULL,
    task_class          TEXT    NOT NULL,
    items_injected      INTEGER NOT NULL DEFAULT 0,
    items_suppressed    INTEGER NOT NULL DEFAULT 0,
    payload_chars       INTEGER NOT NULL DEFAULT 0,
    items_json          TEXT    NOT NULL DEFAULT '[]',
    suppressed_json     TEXT    NOT NULL DEFAULT '[]',
    injected_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_injection_dispatch
    ON intelligence_injections (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_injection_point
    ON intelligence_injections (injection_point, injected_at DESC);
CREATE INDEX IF NOT EXISTS idx_injection_task_class
    ON intelligence_injections (task_class);

-- ============================================================================
-- INBOUND INBOX
-- ============================================================================
-- Durable inbox for inbound channel/event payloads.
-- Events land here before being translated into dispatches.
--
-- Inbox states:
--   received    — event persisted, not yet processed
--   processing  — translation to dispatch in progress
--   dispatched  — canonical dispatch created
--   rejected    — event rejected (invalid, duplicate, etc.)
--   dead_letter — processing failed after retry budget exhausted

CREATE TABLE IF NOT EXISTS inbound_inbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            TEXT    NOT NULL UNIQUE,
    channel_id          TEXT    NOT NULL,
    dedupe_key          TEXT    NOT NULL,
    state               TEXT    NOT NULL DEFAULT 'received',
    payload_json        TEXT    NOT NULL,
    routing_hints_json  TEXT    DEFAULT '{}',
    dispatch_id         TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 3,
    failure_reason      TEXT,
    received_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed_at        TEXT,
    metadata_json       TEXT    DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedupe
    ON inbound_inbox (channel_id, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_inbox_state
    ON inbound_inbox (state, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_dispatch
    ON inbound_inbox (dispatch_id);

-- ============================================================================
-- RECOMMENDATIONS
-- ============================================================================
-- Operator-facing advisory recommendations derived from intelligence analysis.
--
-- Recommendation classes:
--   prompt_patch | routing_preference | guardrail_adjustment | process_improvement
--
-- Acceptance states:
--   proposed | accepted | rejected | expired | superseded

CREATE TABLE IF NOT EXISTS recommendations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id       TEXT    NOT NULL UNIQUE,
    recommendation_class    TEXT    NOT NULL,
    title                   TEXT    NOT NULL,
    description             TEXT    NOT NULL,
    evidence_summary        TEXT    NOT NULL,
    confidence              REAL    NOT NULL DEFAULT 0.0,
    scope_tags_json         TEXT    NOT NULL DEFAULT '[]',
    acceptance_state        TEXT    NOT NULL DEFAULT 'proposed',
    proposed_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    accepted_at             TEXT,
    rejected_at             TEXT,
    rejection_reason        TEXT,
    expired_at              TEXT,
    superseded_by           TEXT,
    outcome_window_start    TEXT,
    outcome_window_end      TEXT,
    metadata_json           TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_rec_class
    ON recommendations (recommendation_class, acceptance_state);
CREATE INDEX IF NOT EXISTS idx_rec_state
    ON recommendations (acceptance_state, proposed_at DESC);
CREATE INDEX IF NOT EXISTS idx_rec_scope
    ON recommendations (acceptance_state);

-- ============================================================================
-- RECOMMENDATION OUTCOMES
-- ============================================================================
-- Stores before/after metric comparisons for accepted recommendations.
-- One row per (recommendation_id, metric_name).

CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id       TEXT    NOT NULL REFERENCES recommendations (recommendation_id),
    metric_name             TEXT    NOT NULL,
    baseline_value          REAL,
    baseline_sample_size    INTEGER NOT NULL DEFAULT 0,
    outcome_value           REAL,
    outcome_sample_size     INTEGER NOT NULL DEFAULT 0,
    delta                   REAL,
    direction               TEXT,
    comparison_status       TEXT    NOT NULL DEFAULT 'pending',
    computed_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(recommendation_id, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_outcome_rec
    ON recommendation_outcomes (recommendation_id);
CREATE INDEX IF NOT EXISTS idx_outcome_metric
    ON recommendation_outcomes (metric_name, comparison_status);

-- ============================================================================
-- SEED: initial execution targets for existing terminals
-- ============================================================================
-- Register current interactive tmux targets. These map to the existing
-- terminal_leases rows. Health starts as 'offline' — the runtime sets
-- health to 'healthy' when the terminal is confirmed responsive.

INSERT OR IGNORE INTO execution_targets (target_id, target_type, terminal_id, capabilities_json, health, model)
VALUES
    ('interactive_tmux_claude_T1', 'interactive_tmux_claude', 'T1',
     '["coding_interactive","research_structured","docs_synthesis","ops_watchdog"]',
     'offline', 'sonnet'),
    ('interactive_tmux_claude_T2', 'interactive_tmux_claude', 'T2',
     '["coding_interactive","research_structured","docs_synthesis","ops_watchdog"]',
     'offline', 'sonnet'),
    ('interactive_tmux_claude_T3', 'interactive_tmux_claude', 'T3',
     '["coding_interactive","research_structured","docs_synthesis","ops_watchdog"]',
     'offline', 'opus');

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (4, 'FP-C PR-0: execution targets, task class routing, intelligence injection, inbound inbox, recommendations');
