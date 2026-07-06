-- VNX Migration 0031 — ADR-007 runtime tenant + FK repair
--
-- Repairs central runtime tables left behind by the dispatches v22 rebuild.
-- ADR-007 requires project_id on every central tenant table and composite
-- UNIQUE/PK constraints over tenant natural keys:
-- docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
--
-- Crash safety/idempotency are owned by apply_migration_v31. The runner verifies
-- the exact legacy shape, disables foreign_keys before BEGIN IMMEDIATE, executes
-- this static DDL atomically, and checks foreign_key_check + integrity_check
-- before commit. The lease-token partial UNIQUE remains intentionally global
-- because it is an incarnation token, not a tenant natural key.
--
-- TENANT PLACEHOLDER (ADR-007, D3): the ``'vnx-dev'`` literals below (the four
-- project_id column DEFAULTs and the four row-copy INSERT...SELECT projections)
-- are AT-REST PLACEHOLDERS ONLY. apply_migration_v31 renders this file through
-- _render_static_0031_sql_with_pid(), substituting the DB-path-anchored,
-- fail-closed-RESOLVED project_id before execution, so a non-vnx-dev store is
-- never stamped 'vnx-dev'. Do NOT read these literals as a hardcoded tenant.

PRAGMA foreign_keys = OFF;

-- dispatch_attempts: tenant stamp + composite dispatch FK/natural key.
DROP TABLE IF EXISTS dispatch_attempts_new;

CREATE TABLE dispatch_attempts_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      TEXT    NOT NULL,
    dispatch_id     TEXT    NOT NULL,
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    terminal_id     TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at        TEXT,
    failure_reason  TEXT,
    metadata_json   TEXT    DEFAULT '{}',
    UNIQUE(attempt_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
);

INSERT INTO dispatch_attempts_new (
    id, attempt_id, dispatch_id, project_id, attempt_number, terminal_id,
    state, started_at, ended_at, failure_reason, metadata_json
)
SELECT
    id, attempt_id, dispatch_id, 'vnx-dev', attempt_number, terminal_id,
    state, started_at, ended_at, failure_reason, metadata_json
FROM dispatch_attempts;

DROP TABLE dispatch_attempts;
ALTER TABLE dispatch_attempts_new RENAME TO dispatch_attempts;

CREATE INDEX idx_attempt_dispatch
    ON dispatch_attempts(dispatch_id, attempt_number);
CREATE INDEX idx_attempt_state
    ON dispatch_attempts(state, started_at DESC);
CREATE INDEX idx_attempt_terminal
    ON dispatch_attempts(terminal_id, started_at DESC);
CREATE INDEX idx_attempt_project
    ON dispatch_attempts(project_id);

-- headless_runs: tenant stamp + composite dispatch/attempt FKs/natural key.
DROP TABLE IF EXISTS headless_runs_new;

CREATE TABLE headless_runs_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL,
    dispatch_id             TEXT    NOT NULL,
    project_id              TEXT    NOT NULL DEFAULT 'vnx-dev',
    attempt_id              TEXT    NOT NULL,
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
    metadata_json           TEXT    DEFAULT '{}',
    UNIQUE(run_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id),
    FOREIGN KEY (attempt_id, project_id)
        REFERENCES dispatch_attempts(attempt_id, project_id)
);

INSERT INTO headless_runs_new (
    id, run_id, dispatch_id, project_id, attempt_id, target_id, target_type,
    task_class, terminal_id, pid, pgid, state, failure_class, exit_code,
    started_at, subprocess_started_at, heartbeat_at, last_output_at,
    completed_at, duration_seconds, log_artifact_path, output_artifact_path,
    receipt_id, metadata_json
)
SELECT
    id, run_id, dispatch_id, 'vnx-dev', attempt_id, target_id, target_type,
    task_class, terminal_id, pid, pgid, state, failure_class, exit_code,
    started_at, subprocess_started_at, heartbeat_at, last_output_at,
    completed_at, duration_seconds, log_artifact_path, output_artifact_path,
    receipt_id, metadata_json
FROM headless_runs;

DROP TABLE headless_runs;
ALTER TABLE headless_runs_new RENAME TO headless_runs;

CREATE INDEX idx_headless_run_state
    ON headless_runs(state, started_at DESC);
CREATE INDEX idx_headless_run_dispatch
    ON headless_runs(dispatch_id);
CREATE INDEX idx_headless_run_target
    ON headless_runs(target_id, state);
CREATE INDEX idx_headless_run_heartbeat
    ON headless_runs(state, heartbeat_at)
    WHERE state = 'running';
CREATE INDEX idx_headless_run_project
    ON headless_runs(project_id);

-- terminal_leases: tenant stamp + composite dispatch FK/natural key.
DROP TABLE IF EXISTS terminal_leases_new;

CREATE TABLE terminal_leases_new (
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
    lease_token         TEXT    NOT NULL DEFAULT '',
    UNIQUE(terminal_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
);

INSERT INTO terminal_leases_new (
    id, terminal_id, project_id, state, dispatch_id, generation, leased_at,
    expires_at, last_heartbeat_at, released_at, worker_pid, metadata_json,
    lease_token
)
SELECT
    id, terminal_id, 'vnx-dev', state, dispatch_id, generation, leased_at,
    expires_at, last_heartbeat_at, released_at, worker_pid, metadata_json,
    lease_token
FROM terminal_leases;

DROP TABLE terminal_leases;
ALTER TABLE terminal_leases_new RENAME TO terminal_leases;

CREATE INDEX idx_lease_state
    ON terminal_leases(state);
CREATE INDEX idx_lease_dispatch
    ON terminal_leases(dispatch_id);
CREATE INDEX idx_lease_project
    ON terminal_leases(project_id);
CREATE INDEX idx_lease_terminal_project
    ON terminal_leases(terminal_id, project_id);
CREATE UNIQUE INDEX idx_terminal_leases_token
    ON terminal_leases(lease_token)
    WHERE lease_token != '';

-- worker_states: tenant stamp + composite PK and runtime-parent FKs.
DROP TABLE IF EXISTS worker_states_new;

CREATE TABLE worker_states_new (
    terminal_id      TEXT    NOT NULL,
    project_id       TEXT    NOT NULL DEFAULT 'vnx-dev',
    dispatch_id      TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'initializing',
    last_output_at   TEXT,
    state_entered_at TEXT    NOT NULL,
    stall_count      INTEGER NOT NULL DEFAULT 0,
    blocked_reason   TEXT,
    metadata_json    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (terminal_id, project_id),
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
);

INSERT INTO worker_states_new (
    terminal_id, project_id, dispatch_id, state, last_output_at,
    state_entered_at, stall_count, blocked_reason, metadata_json,
    created_at, updated_at
)
SELECT
    terminal_id, 'vnx-dev', dispatch_id, state, last_output_at,
    state_entered_at, stall_count, blocked_reason, metadata_json,
    created_at, updated_at
FROM worker_states;

DROP TABLE worker_states;
ALTER TABLE worker_states_new RENAME TO worker_states;

CREATE INDEX idx_worker_state
    ON worker_states(state);
CREATE INDEX idx_worker_dispatch
    ON worker_states(dispatch_id);
CREATE INDEX idx_worker_states_project
    ON worker_states(project_id);

PRAGMA user_version = 31;

PRAGMA foreign_keys = ON;
