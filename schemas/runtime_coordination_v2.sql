-- VNX Runtime Coordination Schema — Migration v2 (PR-1)
-- Purpose: Durable incident log, retry budgets, and cooldown shadow path.
-- Applies on top of: v1 (PR-0) — dispatches, attempts, leases, events
--
-- Design notes:
--   - incident_log stores typed incident records (from incident_taxonomy.py)
--   - retry_budgets persists per-entity retry state across process restarts
--   - Shadow mode: records supervisor outcomes without changing restart behavior
--   - All tables use CREATE TABLE IF NOT EXISTS for idempotency
--
-- Governance:
--   G-R2: retry budgets enforce bounded retries (no infinite loops)
--   G-R3: every recovery action must emit an incident trail

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- INCIDENT LOG
-- ============================================================================
-- Durable typed incident records. One row per incident occurrence.
-- State may be updated (open → resolved / escalated / dead_lettered)
-- but the row is never deleted.
--
-- Incident classes: process_crash | terminal_unresponsive | delivery_failure
--                   ack_timeout | lease_conflict | resume_failed
--                   | repeated_failure_loop
--
-- Incident states:
--   open          — detected, recovery in progress or pending
--   resolved      — resolved (process restarted, dispatch recovered, etc.)
--   escalated     — escalated to T0/operator; auto-recovery may continue
--   dead_lettered — dispatch entered dead-letter; no further auto-retry

CREATE TABLE IF NOT EXISTS incident_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id             TEXT    NOT NULL UNIQUE,
    incident_class          TEXT    NOT NULL,
    severity                TEXT    NOT NULL,
    entity_type             TEXT    NOT NULL,  -- 'dispatch' | 'terminal' | 'component'
    entity_id               TEXT    NOT NULL,  -- dispatch_id | terminal_id | component name
    dispatch_id             TEXT,              -- informational FK (no hard constraint)
    terminal_id             TEXT,
    component_name          TEXT,              -- supervised process name if applicable
    state                   TEXT    NOT NULL DEFAULT 'open',
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    budget_exhausted        INTEGER NOT NULL DEFAULT 0,  -- boolean 0/1
    escalated               INTEGER NOT NULL DEFAULT 0,  -- boolean 0/1
    auto_recovery_halted    INTEGER NOT NULL DEFAULT 0,  -- boolean 0/1
    failure_detail          TEXT,
    actor                   TEXT    NOT NULL DEFAULT 'supervisor',
    occurred_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at             TEXT,
    metadata_json           TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_incident_class_state
    ON incident_log (incident_class, state, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_entity
    ON incident_log (entity_type, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_dispatch
    ON incident_log (dispatch_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_terminal
    ON incident_log (terminal_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_state_occurred
    ON incident_log (state, occurred_at DESC);

-- ============================================================================
-- RETRY BUDGETS
-- ============================================================================
-- Persistent retry bookkeeping per (entity_type, entity_id, incident_class).
-- One row per unique combination; updated in-place on each recovery attempt.
-- Survives process restarts — canonical budget state.
--
-- budget_key format: '{entity_type}:{entity_id}:{incident_class}'
--   e.g. 'component:dispatcher:process_crash'
--        'dispatch:20260329-001:delivery_failure'
--        'terminal:T2:terminal_unresponsive'

CREATE TABLE IF NOT EXISTS retry_budgets (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_key              TEXT    NOT NULL UNIQUE,
    entity_type             TEXT    NOT NULL,
    entity_id               TEXT    NOT NULL,
    incident_class          TEXT    NOT NULL,
    attempts_used           INTEGER NOT NULL DEFAULT 0,
    max_retries             INTEGER NOT NULL,
    cooldown_seconds        INTEGER NOT NULL DEFAULT 0,
    backoff_factor          REAL    NOT NULL DEFAULT 1.0,
    max_cooldown_seconds    INTEGER NOT NULL DEFAULT 600,
    last_attempt_at         TEXT,
    next_allowed_at         TEXT,   -- NULL = no active cooldown
    escalated_at            TEXT,
    auto_recovery_halted    INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_budget_entity
    ON retry_budgets (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_budget_class
    ON retry_budgets (incident_class);
CREATE INDEX IF NOT EXISTS idx_budget_next_allowed
    ON retry_budgets (next_allowed_at);
CREATE INDEX IF NOT EXISTS idx_budget_halted
    ON retry_budgets (auto_recovery_halted);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (2, 'PR-1: durable incident log, retry budgets, cooldown shadow path');
