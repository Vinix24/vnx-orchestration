-- VNX Migration 0017 — Wave 5 PR-5.3 multi-tenant lease isolation
-- Purpose: Composite UNIQUE constraints on tenant-scoped hot tables;
--          project_id added to worker_states (missed in v9 Feature 12 PR-1).
--
-- Design: claudedocs/wave5-control-centre-architecture.md §6 PR-5.3
-- ADR: ADR-017 Control Centre product-shape architecture
--
-- Atomicity: single BEGIN/COMMIT transaction; FK enforcement suspended for
--            table-rebuild steps. On error, the uncommitted transaction is
--            rolled back when the connection is closed.
--
-- Idempotency: guarded by the Python runner (scripts/lib/migrations/apply_0017.py)
--              which checks runtime_schema_version before executing this script.
--              The final INSERT OR IGNORE is a defence-in-depth guard only.
--
-- Applied by: scripts/lib/migrations/apply_0017.py
-- Tested by:  tests/test_schema_0017_migration.py
--
-- Pre-migration state (v11, after 0010 + 0015):
--   terminal_leases  — has project_id, UNIQUE(terminal_id) only
--   dispatches       — has project_id, UNIQUE(dispatch_id) only
--   worker_states    — no project_id column
--
-- Post-migration state (v12):
--   terminal_leases  — UNIQUE(terminal_id, project_id)
--   dispatches       — UNIQUE(dispatch_id, project_id)
--   worker_states    — project_id TEXT NOT NULL DEFAULT 'vnx-dev'

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- 1. worker_states: add project_id (missed in v9)
-- ============================================================================

ALTER TABLE worker_states ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';
CREATE INDEX IF NOT EXISTS idx_worker_states_project ON worker_states(project_id);

-- ============================================================================
-- 2. terminal_leases: composite UNIQUE(terminal_id, project_id)
--    SQLite cannot ALTER a constraint — requires table rebuild.
-- ============================================================================

CREATE TABLE terminal_leases_v10 (
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
    metadata_json       TEXT    DEFAULT '{}',
    FOREIGN KEY (dispatch_id, project_id) REFERENCES dispatches(dispatch_id, project_id),
    UNIQUE(terminal_id, project_id)
);

INSERT INTO terminal_leases_v10
    SELECT id, terminal_id, project_id, state, dispatch_id, generation,
           leased_at, expires_at, last_heartbeat_at, released_at, metadata_json
    FROM terminal_leases;

DROP TABLE terminal_leases;
ALTER TABLE terminal_leases_v10 RENAME TO terminal_leases;

CREATE INDEX IF NOT EXISTS idx_lease_state             ON terminal_leases(state);
CREATE INDEX IF NOT EXISTS idx_lease_dispatch          ON terminal_leases(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_lease_project           ON terminal_leases(project_id);
CREATE INDEX IF NOT EXISTS idx_lease_terminal_project  ON terminal_leases(terminal_id, project_id);

-- ============================================================================
-- 3. dispatches: composite UNIQUE(dispatch_id, project_id)
--    Composite rather than natural-key only — prevents cross-tenant collision
--    when dispatch_id prefixes are reused under ADR-007 §3.
-- ============================================================================

CREATE TABLE dispatches_v10 (
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

INSERT INTO dispatches_v10 SELECT * FROM dispatches;

DROP TABLE dispatches;
ALTER TABLE dispatches_v10 RENAME TO dispatches;

CREATE INDEX IF NOT EXISTS idx_dispatch_state    ON dispatches(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_terminal ON dispatches(terminal_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_created  ON dispatches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatches_project ON dispatches(project_id);

-- ============================================================================
-- 3.5. dispatch_attempts: fix broken FK after dispatches composite UNIQUE
--      REFERENCES dispatches(dispatch_id) is now invalid — upgrade to composite.
--      project_id already present (added by migration 0010).
-- ============================================================================

CREATE TABLE dispatch_attempts_v10 (
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
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
    FOREIGN KEY (dispatch_id, project_id) REFERENCES dispatches(dispatch_id, project_id)
);

INSERT INTO dispatch_attempts_v10
    SELECT id, attempt_id, dispatch_id, attempt_number, terminal_id,
           state, started_at, ended_at, failure_reason, metadata_json, project_id
    FROM dispatch_attempts;

DROP TABLE dispatch_attempts;
ALTER TABLE dispatch_attempts_v10 RENAME TO dispatch_attempts;

CREATE INDEX IF NOT EXISTS idx_attempt_dispatch  ON dispatch_attempts(dispatch_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_attempt_state     ON dispatch_attempts(state, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_terminal  ON dispatch_attempts(terminal_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempt_project   ON dispatch_attempts(project_id);

-- ============================================================================
-- 4. Version stamp
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (12, 'Wave 5 PR-5.3: composite UNIQUE on terminal_leases + dispatches; project_id on worker_states');

COMMIT;

PRAGMA foreign_keys = ON;
