-- VNX Runtime Coordination Schema — Migration v3 (PR-2 draft)
-- Purpose: Workflow supervisor tables — retry_state, escalation_log.
-- Applies on top of: v2 (PR-1) — incident_log and retry_budgets already exist
--
-- NOTE: incident_log is owned by v2 (PR-1). This migration does NOT re-create
-- it. PR-2 workflow supervisor uses the incident_log from v2 directly.
--
-- Design notes:
--   - retry_state tracks per-dispatch retry budgets (workflow supervisor view)
--   - escalation_log records escalation events to T0/operator
--   - All tables use CREATE TABLE IF NOT EXISTS for idempotency

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- RETRY STATE
-- ============================================================================
-- Per-dispatch retry budget tracking. One row per (dispatch_id, incident_class).
-- Updated on each retry attempt; never deleted, only reset on re-queue.

CREATE TABLE IF NOT EXISTS retry_state (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id         TEXT    NOT NULL,
    incident_class      TEXT    NOT NULL,
    attempts_used       INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     TEXT,
    next_eligible_at    TEXT,
    budget_exhausted    INTEGER NOT NULL DEFAULT 0,
    escalated           INTEGER NOT NULL DEFAULT 0,
    halted              INTEGER NOT NULL DEFAULT 0,
    metadata_json       TEXT    DEFAULT '{}',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(dispatch_id, incident_class)
);

CREATE INDEX IF NOT EXISTS idx_retry_dispatch
    ON retry_state (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_retry_exhausted
    ON retry_state (budget_exhausted, dispatch_id);

-- ============================================================================
-- ESCALATION LOG
-- ============================================================================
-- Records escalation events to T0/operator. Append-only.

CREATE TABLE IF NOT EXISTS escalation_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    escalation_id       TEXT    NOT NULL UNIQUE,
    incident_id         TEXT    NOT NULL,
    dispatch_id         TEXT,
    terminal_id         TEXT,
    incident_class      TEXT    NOT NULL,
    severity            TEXT    NOT NULL,
    escalated_to        TEXT    NOT NULL DEFAULT 'T0',
    reason              TEXT    NOT NULL,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    budget_exhausted    INTEGER NOT NULL DEFAULT 0,
    auto_recovery_halted INTEGER NOT NULL DEFAULT 0,
    acknowledged        INTEGER NOT NULL DEFAULT 0,
    acknowledged_at     TEXT,
    metadata_json       TEXT    DEFAULT '{}',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_escalation_dispatch
    ON escalation_log (dispatch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_escalation_unacked
    ON escalation_log (acknowledged, created_at DESC);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (3, 'PR-2 draft: workflow supervisor — retry_state, escalation_log');
