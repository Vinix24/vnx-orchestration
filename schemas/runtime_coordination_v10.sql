-- VNX Runtime Coordination Schema — v10 baseline (Wave 5 PR-5.3)
-- Purpose: Multi-tenant lease isolation — composite UNIQUE constraints on
--          terminal_leases and dispatches; project_id on worker_states.
-- Applies on top of: v9 (Feature 12 PR-1) + migrations 0010 + 0015.
--
-- Design: claudedocs/wave5-control-centre-architecture.md §6 PR-5.3
-- Applied by migration: schemas/migrations/0017_multi_tenant_lease_isolation.sql
--
-- Key changes from v9:
--   terminal_leases — UNIQUE(terminal_id) replaced by UNIQUE(terminal_id, project_id)
--   dispatches      — UNIQUE(dispatch_id) replaced by UNIQUE(dispatch_id, project_id)
--   worker_states   — project_id column added (missed in v9)
--
-- This file documents the target schema structure. For fresh DB creation,
-- apply runtime_coordination.sql then all migrations 0010 through 0017.
--
-- SCHEMA-ORDER NOTE (fix: PR-MIGRATOR-FIX 2026-05-24):
-- The CREATE TABLE IF NOT EXISTS statements below are no-ops on a fresh install
-- because the base tables (dispatches, terminal_leases, worker_states) are
-- created by runtime_coordination.sql (v1) and runtime_coordination_v9.sql
-- WITHOUT a project_id column. The project_id column is added by migration
-- 0010 (dispatches, terminal_leases, etc.) and migration 0017 (worker_states).
-- Those migrations always run AFTER coordination_db.init_schema completes.
--
-- Therefore: CREATE INDEX statements that reference project_id are intentionally
-- ABSENT from this file. They are created by migrations 0010 and 0017 which
-- run after init_schema via the migrator or apply_0017.py. Adding them here
-- caused "no such column: project_id" on fresh --fresh-central installs because
-- project_id does not exist yet when init_schema runs v10 on a fresh DB.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- TERMINAL LEASES (v10 — composite UNIQUE)
-- ============================================================================
-- One row per (terminal, project) pair. Multiple projects can each have a
-- row for T1/T2/T3 without collision. The composite UNIQUE enforces that
-- the same terminal cannot be leased by two dispatches in the same project.

CREATE TABLE IF NOT EXISTS terminal_leases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
    state               TEXT    NOT NULL DEFAULT 'idle',
    dispatch_id         TEXT,
    generation          INTEGER NOT NULL DEFAULT 1,
    leased_at           TEXT,
    expires_at          TEXT,
    last_heartbeat_at   TEXT,
    released_at         TEXT,
    worker_pid          INTEGER,
    metadata_json       TEXT    DEFAULT '{}',
    FOREIGN KEY (dispatch_id, project_id) REFERENCES dispatches(dispatch_id, project_id),
    UNIQUE(terminal_id, project_id)
);

-- worker_pid (INTEGER, nullable): OS PID of the worker process currently
-- attached to this terminal, or NULL when none. Written by
-- pool_state_repo.store_worker_pid() (called from subprocess_dispatch) and read
-- by the lease-reaper / runtime_supervise path. On a DB that predates this
-- column it is self-healed idempotently by the init path
-- (project_id_migration.ensure_worker_pid_column), independent of user_version.

-- Non-project_id indexes are safe here because these columns exist in v1.
-- project_id indexes (idx_lease_project, idx_lease_terminal_project) are
-- created by migrations 0010 and 0017 after project_id is added.
CREATE INDEX IF NOT EXISTS idx_lease_state            ON terminal_leases(state);
CREATE INDEX IF NOT EXISTS idx_lease_dispatch         ON terminal_leases(dispatch_id);

-- ============================================================================
-- DISPATCHES (v10 — composite UNIQUE)
-- ============================================================================
-- UNIQUE(dispatch_id, project_id) rather than UNIQUE(dispatch_id) alone.
-- Prevents cross-project collision under ADR-007 §3 identifier-reuse rules.

CREATE TABLE IF NOT EXISTS dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL,
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
    metadata_json   TEXT    DEFAULT '{}',
    UNIQUE(dispatch_id, project_id)
);

-- Non-project_id indexes are safe here (columns exist from v1 schema).
-- idx_dispatches_project is created by migration 0010 after project_id is added.
CREATE INDEX IF NOT EXISTS idx_dispatch_state    ON dispatches(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_terminal ON dispatches(terminal_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_created  ON dispatches(created_at DESC);

-- ============================================================================
-- WORKER STATES (v10 — project_id added)
-- ============================================================================
-- project_id was missing from the v9 schema (Feature 12 PR-1). Added here
-- with DEFAULT 'vnx-dev' to preserve backward compatibility.

CREATE TABLE IF NOT EXISTS worker_states (
    terminal_id      TEXT    NOT NULL,
    dispatch_id      TEXT    NOT NULL,
    project_id       TEXT    NOT NULL DEFAULT 'vnx-dev',
    state            TEXT    NOT NULL DEFAULT 'initializing',
    last_output_at   TEXT,
    state_entered_at TEXT    NOT NULL,
    stall_count      INTEGER NOT NULL DEFAULT 0,
    blocked_reason   TEXT,
    metadata_json    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (terminal_id)
);

-- Non-project_id indexes are safe here (columns exist from v9 schema).
-- idx_worker_states_project is created by migration 0017 after project_id is added.
CREATE INDEX IF NOT EXISTS idx_worker_state   ON worker_states(state);
CREATE INDEX IF NOT EXISTS idx_worker_dispatch ON worker_states(dispatch_id);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (12, 'Wave 5 PR-5.3: composite UNIQUE on terminal_leases + dispatches; project_id on worker_states');

-- ============================================================================
-- SCHEMA META (Wave 2a: schema version tracking for migration safety)
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '0');
