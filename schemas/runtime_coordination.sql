-- VNX Runtime Coordination Schema
-- Version: 1 (PR-0)
-- Purpose: Canonical coordination model for dispatch registry, terminal leases,
--          dispatch attempts, and append-only coordination events.
-- Database: .vnx-data/state/runtime_coordination.db (SQLite)
--
-- Design notes:
--   - terminal_state.json is a DERIVED PROJECTION of terminal_leases — not the source of truth.
--   - panes.json is an ADAPTER MAPPING for tmux pane IDs — not the source of truth for ownership.
--   - Every state transition must append a row to coordination_events.
--   - Schema initialization is idempotent: CREATE TABLE IF NOT EXISTS throughout.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- SCHEMA VERSIONING
-- ============================================================================

CREATE TABLE IF NOT EXISTS runtime_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT NOT NULL
);

-- ============================================================================
-- DISPATCH REGISTRY
-- ============================================================================
-- Canonical record of every dispatch from creation through terminal outcome.
--
-- Canonical dispatch states:
--   queued          — registered, not yet assigned to a terminal
--   claimed         — terminal lease acquired, delivery not started
--   delivering      — dispatch bundle is being sent to terminal
--   accepted        — terminal has ACKed receipt of the dispatch
--   running         — worker has started executing
--   completed       — worker reported success (T0 completion authority unchanged)
--   timed_out       — no ACK or heartbeat within deadline
--   failed_delivery — delivery transport failed (tmux error, pane gone, etc.)
--   expired         — exceeded max attempt window without resolution
--   recovered       — reconciler moved from timed_out/failed_delivery to recoverable

CREATE TABLE IF NOT EXISTS dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL UNIQUE,
    state           TEXT    NOT NULL DEFAULT 'queued',
    terminal_id     TEXT,
    track           TEXT,
    priority        TEXT    DEFAULT 'P2',
    pr_ref          TEXT,
    gate            TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    bundle_path     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_after   TEXT,
    metadata_json   TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_dispatch_state
    ON dispatches (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_terminal
    ON dispatches (terminal_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_created
    ON dispatches (created_at DESC);

-- ============================================================================
-- DISPATCH ATTEMPTS
-- ============================================================================
-- One row per delivery attempt. A dispatch may have multiple attempts if
-- delivery fails and the dispatch is recovered.
--
-- Attempt event types (recorded in coordination_events, not duplicated here):
--   claim | deliver_start | deliver_success | deliver_failure | accepted | timed_out

CREATE TABLE IF NOT EXISTS dispatch_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      TEXT    NOT NULL UNIQUE,
    dispatch_id     TEXT    NOT NULL REFERENCES dispatches (dispatch_id),
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    terminal_id     TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at        TEXT,
    failure_reason  TEXT,
    metadata_json   TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_attempt_dispatch
    ON dispatch_attempts (dispatch_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_attempt_state
    ON dispatch_attempts (state, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_terminal
    ON dispatch_attempts (terminal_id, started_at DESC);

-- ============================================================================
-- TERMINAL LEASES
-- ============================================================================
-- Canonical ownership record for each worker terminal (T1/T2/T3).
-- One row per terminal; updated in-place on every lease operation.
--
-- Canonical lease states:
--   idle        — terminal is available for assignment
--   leased      — terminal is owned by a dispatch
--   expired     — lease TTL elapsed without heartbeat; awaiting recovery
--   recovering  — reconciler has started recovery process
--   released    — lease was explicitly released (terminal returned to idle)
--
-- Generation field prevents stale-reclaim races: a reclaim must present
-- the current generation value to succeed.

CREATE TABLE IF NOT EXISTS terminal_leases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL UNIQUE,
    state               TEXT    NOT NULL DEFAULT 'idle',
    dispatch_id         TEXT    REFERENCES dispatches (dispatch_id),
    generation          INTEGER NOT NULL DEFAULT 1,
    leased_at           TEXT,
    expires_at          TEXT,
    last_heartbeat_at   TEXT,
    released_at         TEXT,
    metadata_json       TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lease_state
    ON terminal_leases (state);
CREATE INDEX IF NOT EXISTS idx_lease_dispatch
    ON terminal_leases (dispatch_id);

-- ============================================================================
-- COORDINATION EVENTS (append-only)
-- ============================================================================
-- Immutable event log. Every state transition in dispatches or terminal_leases
-- must append a row here. Used for audit, reconciliation, and debugging.
--
-- event_type vocabulary:
--   dispatch_queued | dispatch_claimed | dispatch_delivering | dispatch_accepted
--   dispatch_running | dispatch_completed | dispatch_timed_out
--   dispatch_failed_delivery | dispatch_expired | dispatch_recovered
--   lease_acquired | lease_renewed | lease_released | lease_expired | lease_recovered
--   attempt_created | attempt_succeeded | attempt_failed | attempt_timed_out
--   reconciliation_run | reconciliation_recovered | reconciliation_expired

CREATE TABLE IF NOT EXISTS coordination_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL UNIQUE,
    event_type      TEXT    NOT NULL,
    entity_type     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    actor           TEXT    NOT NULL DEFAULT 'runtime',
    reason          TEXT,
    metadata_json   TEXT    DEFAULT '{}',
    occurred_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_event_entity
    ON coordination_events (entity_type, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_type
    ON coordination_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_occurred
    ON coordination_events (occurred_at DESC);

-- ============================================================================
-- SEED: initial terminal lease rows for T1, T2, T3
-- ============================================================================
-- These rows are created once. Later updates are done via UPDATE, never INSERT
-- (one row per terminal at all times, enforced by UNIQUE on terminal_id).

INSERT OR IGNORE INTO terminal_leases (terminal_id, state, generation)
VALUES
    ('T1', 'idle', 1),
    ('T2', 'idle', 1),
    ('T3', 'idle', 1);

-- ============================================================================
-- SEED: schema version record
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (1, 'PR-0: initial runtime coordination schema — dispatches, attempts, leases, events');
