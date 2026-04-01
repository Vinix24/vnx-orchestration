-- VNX Runtime Coordination Schema — Migration v9 (Feature 12, PR-1)
-- Purpose: Add worker_states table for canonical worker lifecycle tracking.
-- Contract: docs/core/130_RUNTIME_STATE_MACHINE_CONTRACT.md §8.1
-- Applies on top of: v8 (Headless Observability PR-1) — headless run registry
--
-- Design:
--   - Single row per terminal (PK on terminal_id) — only one active worker per terminal.
--   - state_entered_at tracks when the current state was entered (independent of heartbeat).
--   - last_output_at tracks most recent output event (independent of heartbeat).
--   - stall_count accumulates across stall episodes within one dispatch lifecycle.
--   - Foreign keys enforce referential integrity against terminal_leases and dispatches.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- WORKER STATES
-- ============================================================================

CREATE TABLE IF NOT EXISTS worker_states (
    terminal_id      TEXT    NOT NULL,
    dispatch_id      TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'initializing',
    last_output_at   TEXT,
    state_entered_at TEXT    NOT NULL,
    stall_count      INTEGER NOT NULL DEFAULT 0,
    blocked_reason   TEXT,
    metadata_json    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (terminal_id),
    FOREIGN KEY (terminal_id) REFERENCES terminal_leases(terminal_id),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id)
);

CREATE INDEX IF NOT EXISTS idx_worker_state
    ON worker_states (state);
CREATE INDEX IF NOT EXISTS idx_worker_dispatch
    ON worker_states (dispatch_id);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (9, 'Feature 12 PR-1: worker_states table — canonical worker lifecycle and heartbeat persistence');
