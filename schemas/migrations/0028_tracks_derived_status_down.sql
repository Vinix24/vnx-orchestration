-- VNX Migration 0028 (DOWN) — drop tracks.derived_status
--
-- Reverses 0028_tracks_derived_status.sql.
--
-- Pre-state (v28): tracks.derived_status present, idx_tracks_derived_status.
-- Post-state (v27): tracks without derived_status.
--
-- SQLite < 3.35 has no DROP COLUMN, so tracks is rebuilt to the v27 column
-- set (horizon present, derived_status absent). All other data preserved.
--
-- FK safety: same PRAGMA foreign_keys=OFF guard as the 0027 down migration.
-- track_dependencies, track_phase_history, track_open_items retain their rows.

PRAGMA foreign_keys = OFF;

DROP INDEX IF EXISTS idx_tracks_derived_status;

ALTER TABLE tracks RENAME TO tracks_pre_0028_down;

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
    horizon                     TEXT    CHECK (horizon IS NULL OR horizon IN ('now','next','later')),
    PRIMARY KEY (track_id, project_id)
);

INSERT INTO tracks (
    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
    requires_operator_promotion, instruction_template, context_composer_rules,
    pr_ref, trigger_condition, created_at, phase_changed_at, completed_at,
    metadata_json, horizon
)
SELECT
    track_id, project_id, title, goal_state, phase, next_up, sort_order, priority,
    requires_operator_promotion, instruction_template, context_composer_rules,
    pr_ref, trigger_condition, created_at, phase_changed_at, completed_at,
    metadata_json, horizon
FROM tracks_pre_0028_down;

DROP TABLE tracks_pre_0028_down;

CREATE INDEX IF NOT EXISTS idx_tracks_project_phase_nextup
    ON tracks(project_id, phase, next_up DESC, sort_order);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up_per_project
    ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued';

CREATE INDEX IF NOT EXISTS idx_tracks_horizon
    ON tracks(project_id, horizon, sort_order);

PRAGMA user_version = 27;

PRAGMA foreign_keys = ON;
