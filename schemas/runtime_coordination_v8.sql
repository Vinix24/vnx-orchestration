-- VNX Runtime Coordination Schema -- Migration v8 (Headless Observability PR-1)
-- Purpose: Headless run registry -- durable identity, heartbeat, output timestamps,
--          lifecycle state, and failure classification for headless runs.
-- Applies on top of: v7 (FP-D PR-4) -- provenance verification
--
-- Design notes:
--   - One row per headless run (run_id is unique)
--   - State machine: init -> running -> completing/failing -> succeeded/failed:<class>
--   - Heartbeat and last_output_at are updated in-place during running phase
--   - No backward transitions; recovery creates a new run
--   - Compatible with existing dispatch_attempts and coordination_events tables
--
-- Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 3.1
--
-- Governance:
--   A-R1: Every headless run gets a durable run identity
--   A-R2: Run state captures heartbeat and last-output timestamps
--   A-R5: Process control is group-aware (pgid field)
--   C-1:  Uses same dispatch_attempts table (attempt_id FK)
--   C-2:  Uses same coordination_events table
--   C-5:  All fields are additive (no breaking changes)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- HEADLESS RUN REGISTRY
-- ============================================================================
-- Canonical record for every headless run from init through terminal state.
--
-- Lifecycle states:
--   init        -- run identity created, subprocess not yet spawned
--   running     -- subprocess alive, heartbeat and output timestamps updating
--   completing  -- subprocess exited successfully, output being persisted
--   failing     -- subprocess exited abnormally, failure being classified
--   succeeded   -- terminal: output persisted, receipt emitted
--   failed      -- terminal: failure classified, receipt emitted
--
-- Invariants:
--   - run_id is assigned exactly once and never reused (I-1)
--   - Each run links to exactly one dispatch_id and attempt_id (I-2)
--   - No backward transitions; recovery creates a new run (Section 2.2)
--   - heartbeat_at older than 2x interval while running = stale (Section 3.2)
--   - last_output_at older than threshold while running = hang candidate (Section 3.3)

CREATE TABLE IF NOT EXISTS headless_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL UNIQUE,
    dispatch_id             TEXT    NOT NULL REFERENCES dispatches (dispatch_id),
    attempt_id              TEXT    NOT NULL REFERENCES dispatch_attempts (attempt_id),
    target_id               TEXT    NOT NULL,
    target_type             TEXT    NOT NULL,
    task_class              TEXT    NOT NULL,
    terminal_id             TEXT,
    pid                     INTEGER,
    pgid                    INTEGER,
    state                   TEXT    NOT NULL DEFAULT 'init',
    failure_class           TEXT,
    exit_code               INTEGER,
    started_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    subprocess_started_at   TEXT,
    heartbeat_at            TEXT,
    last_output_at          TEXT,
    completed_at            TEXT,
    duration_seconds        REAL,
    log_artifact_path       TEXT,
    output_artifact_path    TEXT,
    receipt_id              TEXT,
    metadata_json           TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_headless_run_state
    ON headless_runs (state, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_headless_run_dispatch
    ON headless_runs (dispatch_id);
CREATE INDEX IF NOT EXISTS idx_headless_run_target
    ON headless_runs (target_id, state);
CREATE INDEX IF NOT EXISTS idx_headless_run_heartbeat
    ON headless_runs (state, heartbeat_at)
    WHERE state = 'running';

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (8, 'Headless Observability PR-1: run registry with heartbeat and output timestamps');
