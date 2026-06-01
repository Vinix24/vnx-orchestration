-- VNX Migration 0027 (DOWN) — drop deliverables view + tracks.horizon
--
-- Reverses 0027_planning_horizon_and_deliverable_view.sql.
--
-- Pre-state (v27): tracks.horizon present, deliverables VIEW + idx_tracks_horizon.
-- Post-state (v26): tracks without horizon, no deliverables view.
--
-- SQLite < 3.35 has no DROP COLUMN, so tracks is rebuilt to the v26 column set
-- (the v24 composite-key schema, which v25/v26 did not alter). Composite PK
-- (track_id, project_id) and all data are preserved via INSERT ... SELECT.
--
-- FK safety: the ALTER TABLE RENAME retargets child FKs (track_dependencies,
-- track_phase_history, track_open_items) to the renamed table. PRAGMA
-- foreign_keys=OFF is required to DROP the renamed table without SQLite
-- rejecting it due to those still-present FK references. This is the standard
-- SQLite table-rebuild guard (same pattern as the 0024 up-migration).
--
-- Idempotency: guarded by the runner, which only invokes this when stepping
--          user_version 27 -> 26.

PRAGMA foreign_keys = OFF;

-- ============================================================================
-- STEP 1: drop the derived view + horizon index (additive surfaces)
-- ============================================================================

DROP VIEW IF EXISTS deliverables;
DROP INDEX IF EXISTS idx_tracks_horizon;

-- ============================================================================
-- STEP 2: rebuild tracks WITHOUT horizon (preserves composite PK + data)
-- ============================================================================

ALTER TABLE tracks RENAME TO tracks_pre_0027_down;

CREATE TABLE tracks (
    track_id                    TEXT    NOT NULL,
    project_id                  TEXT    NOT NULL DEFAULT 'vnx-dev',
    title                       TEXT    NOT NULL,
    goal_state                  TEXT,
    phase                       TEXT    NOT NULL DEFAULT 'queued'
                                        CHECK (phase IN ('queued','active','parked','done')),
    next_up                     INTEGER NOT NULL DEFAULT 0,
    sort_order                  INTEGER NOT NULL DEFAULT 0,
    priority                    TEXT    DEFAULT 'medium',
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template        TEXT,
    context_composer_rules      TEXT    DEFAULT '{}',
    pr_ref                      TEXT,
    trigger_condition           TEXT,
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    phase_changed_at            TEXT,
    completed_at                TEXT,
    metadata_json               TEXT    DEFAULT '{}',
    PRIMARY KEY (track_id, project_id)
);

INSERT INTO tracks (
    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
    requires_operator_promotion, instruction_template, context_composer_rules,
    pr_ref, trigger_condition, created_at, phase_changed_at, completed_at, metadata_json
)
SELECT
    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
    requires_operator_promotion, instruction_template, context_composer_rules,
    pr_ref, trigger_condition, created_at, phase_changed_at, completed_at, metadata_json
FROM tracks_pre_0027_down;

DROP TABLE tracks_pre_0027_down;

-- Restore the v24 track indexes that referenced the rebuilt tracks table
CREATE INDEX IF NOT EXISTS idx_tracks_project_phase_nextup
    ON tracks(project_id, phase, next_up DESC, sort_order);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up_per_project
    ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued';

-- Step schema version back down
PRAGMA user_version = 26;

PRAGMA foreign_keys = ON;
