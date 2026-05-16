-- VNX Migration 0020 — Wave 6 PR-6.2 elastic worker pool
-- Purpose: Per-project pool configuration, runtime state, and membership.
--
-- Design: claudedocs/wave6-workers-n-architecture.md §4
-- ADR: ADR-013 (workers=N as configuration), ADR-018 (elastic worker pool design freeze)
--
-- Pre-migration state (v13, after 0019): terminal_leases with lease_token column.
-- Post-migration state (v14): adds pool_config, worker_pools, worker_pool_membership.
--
-- Atomicity: single BEGIN TRANSACTION / COMMIT. FK enforcement suspended during
--            CREATE TABLE statements (avoids ordering constraints); re-enabled
--            after COMMIT for runtime enforcement.
--
-- Idempotency: guarded by apply_0020.py (checks MAX(version) before execute).
--              CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE as defence-in-depth.
--
-- Applied by: scripts/lib/migrations/apply_0020.py
-- Tested by:  tests/test_schema_0020_migration.py

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- 1. pool_config — declarative per-project pool configuration
--    One row per (project_id, pool_id). Operator edits this; PoolManager reads.
-- ============================================================================

CREATE TABLE IF NOT EXISTS pool_config (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    min_workers         INTEGER NOT NULL DEFAULT 1,
    max_workers         INTEGER NOT NULL DEFAULT 6,
    target_workers      INTEGER NOT NULL DEFAULT 3,
    role_mix_json       TEXT    NOT NULL DEFAULT '["backend-developer"]',
    provider_mix_json   TEXT    NOT NULL DEFAULT '["claude"]',
    scale_policy        TEXT    NOT NULL DEFAULT 'queue_depth_v1',
    cooldown_seconds    INTEGER NOT NULL DEFAULT 120,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(project_id, pool_id),
    CHECK (min_workers >= 0),
    CHECK (max_workers >= min_workers),
    CHECK (target_workers >= min_workers AND target_workers <= max_workers)
);

CREATE INDEX IF NOT EXISTS idx_pool_config_project ON pool_config(project_id);

-- ============================================================================
-- 2. worker_pools — runtime state per pool (updated per PoolManager tick)
--    Tracks live size, health counts, last scale event. FK to pool_config.
-- ============================================================================

CREATE TABLE IF NOT EXISTS worker_pools (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    state               TEXT    NOT NULL DEFAULT 'idle'
                            CHECK (state IN ('idle', 'scaling', 'draining', 'quota_exhausted')),
    current_size        INTEGER NOT NULL DEFAULT 0,
    target_size         INTEGER NOT NULL DEFAULT 0,
    healthy_count       INTEGER NOT NULL DEFAULT 0,
    stuck_count         INTEGER NOT NULL DEFAULT 0,
    last_scaled_at      TEXT,
    last_scale_action   TEXT,
    last_decision_json  TEXT    DEFAULT '{}',
    metadata_json       TEXT    DEFAULT '{}',
    UNIQUE(project_id, pool_id),
    FOREIGN KEY (project_id, pool_id) REFERENCES pool_config(project_id, pool_id)
);

CREATE INDEX IF NOT EXISTS idx_worker_pools_state   ON worker_pools(state);
CREATE INDEX IF NOT EXISTS idx_worker_pools_project ON worker_pools(project_id);

-- ============================================================================
-- 3. worker_pool_membership — join: worker terminal <-> pool
--    One active row per terminal+project (partial unique idx enforces this).
--    released_at NULL = active; non-NULL = historical (soft-delete for audit).
-- ============================================================================

CREATE TABLE IF NOT EXISTS worker_pool_membership (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    provider            TEXT    NOT NULL
                            CHECK (provider IN ('claude', 'codex', 'gemini', 'litellm')),
    role                TEXT    NOT NULL,
    joined_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at         TEXT,
    release_reason      TEXT,
    spawn_generation    INTEGER NOT NULL DEFAULT 1,
    metadata_json       TEXT    DEFAULT '{}',
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (project_id, pool_id)
        REFERENCES pool_config(project_id, pool_id)
);

-- Partial unique: only one active membership per terminal+project at a time.
-- released_at IS NULL rows must be unique on (terminal_id, project_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active
    ON worker_pool_membership(terminal_id, project_id)
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_pool_membership_pool
    ON worker_pool_membership(project_id, pool_id);

-- ============================================================================
-- 4. Bootstrap rows — default pool for backward compat (matches PR-6.1 YAML)
--    INSERT OR IGNORE: safe to re-run; existing rows untouched.
--    pool_config must be inserted before worker_pools (FK dependency).
-- ============================================================================

INSERT OR IGNORE INTO pool_config
    (project_id, pool_id, min_workers, max_workers, target_workers,
     role_mix_json, provider_mix_json)
VALUES ('vnx-dev', 'default', 1, 4, 3,
        '["backend-developer","quality-engineer","architect"]',
        '["claude"]');

INSERT OR IGNORE INTO worker_pools
    (project_id, pool_id, state, current_size, target_size)
VALUES ('vnx-dev', 'default', 'idle', 0, 3);

-- ============================================================================
-- 5. Version stamp
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (14, 'Wave 6 PR-6.2: pool_config + worker_pools + worker_pool_membership');

COMMIT;

PRAGMA foreign_keys = ON;
