-- VNX Migration 0017 -- DOWN -- multi-tenant lease isolation rollback
-- Reverses 0017_multi_tenant_lease_isolation.sql:
--   - worker_states: drops project_id column + index
--   - dispatches: rebuilds with UNIQUE(dispatch_id) only (drops composite)
--   - dispatch_attempts: rebuilds with single-column FK on dispatch_id
--   - terminal_leases: rebuilds with UNIQUE(terminal_id) only
--   - Removes v12 stamp from runtime_schema_version
--
-- Pre-down state (v12): composite UNIQUE on dispatches + terminal_leases;
--   worker_states has project_id column.
-- Post-down state (v11): single-column UNIQUE(dispatch_id) / UNIQUE(terminal_id);
--   worker_states without project_id.
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.
-- Table rebuilds remove FOREIGN KEY composite constraints.
--
-- CAUTION: Data in worker_states.project_id is lost on rollback.
-- Multi-tenant dispatch isolation is removed after rollback.
--
-- Applied by: operator (manual) — sqlite3 runtime_coordination.db < this_file.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- 1. terminal_leases: rebuild with UNIQUE(terminal_id) only
--    Must drop BEFORE dispatches because it carries FK to dispatches.
-- ============================================================================

CREATE TABLE terminal_leases_v9 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL UNIQUE,
    project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
    state               TEXT    NOT NULL DEFAULT 'idle',
    dispatch_id         TEXT,
    generation          INTEGER NOT NULL DEFAULT 1,
    leased_at           TEXT,
    expires_at          TEXT,
    last_heartbeat_at   TEXT,
    released_at         TEXT,
    metadata_json       TEXT    DEFAULT '{}'
);

INSERT INTO terminal_leases_v9
    SELECT id, terminal_id, project_id, state, dispatch_id, generation,
           leased_at, expires_at, last_heartbeat_at, released_at, metadata_json
    FROM terminal_leases;

DROP TABLE terminal_leases;
ALTER TABLE terminal_leases_v9 RENAME TO terminal_leases;

CREATE INDEX IF NOT EXISTS idx_lease_state    ON terminal_leases(state);
CREATE INDEX IF NOT EXISTS idx_lease_dispatch ON terminal_leases(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_lease_project  ON terminal_leases(project_id);

-- ============================================================================
-- 2. dispatch_attempts: rebuild with single-column FK on dispatch_id
-- ============================================================================

CREATE TABLE dispatch_attempts_v9 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      TEXT    NOT NULL UNIQUE,
    dispatch_id     TEXT    NOT NULL,
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    terminal_id     TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at        TEXT,
    failure_reason  TEXT,
    metadata_json   TEXT    DEFAULT '{}',
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev'
);

INSERT INTO dispatch_attempts_v9
    SELECT id, attempt_id, dispatch_id, attempt_number, terminal_id,
           state, started_at, ended_at, failure_reason, metadata_json, project_id
    FROM dispatch_attempts;

DROP TABLE dispatch_attempts;
ALTER TABLE dispatch_attempts_v9 RENAME TO dispatch_attempts;

CREATE INDEX IF NOT EXISTS idx_attempt_dispatch  ON dispatch_attempts(dispatch_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_attempt_state     ON dispatch_attempts(state, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_terminal  ON dispatch_attempts(terminal_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_project   ON dispatch_attempts(project_id);

-- ============================================================================
-- 3. dispatches: rebuild with UNIQUE(dispatch_id) only
-- ============================================================================

CREATE TABLE dispatches_v9 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL UNIQUE,
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
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

INSERT INTO dispatches_v9 SELECT * FROM dispatches;

DROP TABLE dispatches;
ALTER TABLE dispatches_v9 RENAME TO dispatches;

CREATE INDEX IF NOT EXISTS idx_dispatch_state    ON dispatches(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_terminal ON dispatches(terminal_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_created  ON dispatches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatches_project ON dispatches(project_id);

-- ============================================================================
-- 4. worker_states: drop project_id column + index
-- ============================================================================

DROP INDEX IF EXISTS idx_worker_states_project;
ALTER TABLE worker_states DROP COLUMN project_id;

-- ============================================================================
-- 5. Remove version stamp
-- ============================================================================

DELETE FROM runtime_schema_version WHERE version = 12;

COMMIT;

PRAGMA foreign_keys = ON;
