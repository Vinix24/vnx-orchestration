-- VNX Migration 0022 — track layer (tracks, phase history, dependencies, open-item links)
--
-- Purpose: Promote dispatches.track to first-class by adding the parent tracks table
--          and related link tables. Extends dispatches with state CHECK + operator gate.
--
-- Design: claudedocs/FUTURE-SYSTEM-DESIGN-2026-05-28.md
--
-- Pre-migration state (v21): central install metadata.
-- Post-migration state (v22): track layer tables + dispatches extensions.
--
-- SQLite caveats:
--   ALTER TABLE cannot add CHECK constraints to existing columns directly.
--   The dispatches table is rebuilt to add the state CHECK and operator_approved_at.
--   Existing data is preserved via INSERT ... SELECT.
--   The new state CHECK includes all legacy states for backward compatibility.
--
-- Idempotency: apply_script_if_below skips the entire script when user_version >= 22.
--   Partial-run safety: SAVEPOINT wraps all statements in apply_script_if_below.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by: tests/test_tracks_schema.py

-- ============================================================================
-- TRACKS
-- ============================================================================

CREATE TABLE IF NOT EXISTS tracks (
    track_id                TEXT    NOT NULL PRIMARY KEY,
    title                   TEXT    NOT NULL,
    goal_state              TEXT    NOT NULL,
    phase                   TEXT    NOT NULL DEFAULT 'queued'
                                    CHECK (phase IN ('queued', 'active', 'parked', 'done')),
    next_up                 INTEGER NOT NULL DEFAULT 0,
    sort_order              INTEGER NOT NULL DEFAULT 0,
    priority                TEXT,
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template    TEXT,
    context_composer_rules  TEXT,
    pr_ref                  TEXT,
    trigger_condition       TEXT,
    project_id              TEXT    NOT NULL DEFAULT 'vnx-dev',
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    phase_changed_at        TEXT,
    completed_at            TEXT,
    metadata_json           TEXT    DEFAULT '{}'
);

-- ============================================================================
-- TRACK PHASE HISTORY
-- ============================================================================

CREATE TABLE IF NOT EXISTS track_phase_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT    NOT NULL REFERENCES tracks(track_id),
    from_phase  TEXT,
    to_phase    TEXT    NOT NULL,
    actor       TEXT    NOT NULL CHECK (actor IN ('operator', 'T0', 'system')),
    reason      TEXT,
    approval_id TEXT,
    occurred_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ============================================================================
-- TRACK DEPENDENCIES
-- ============================================================================

CREATE TABLE IF NOT EXISTS track_dependencies (
    from_track_id       TEXT    NOT NULL REFERENCES tracks(track_id),
    to_track_id         TEXT    NOT NULL REFERENCES tracks(track_id),
    kind                TEXT    NOT NULL CHECK (kind IN ('hard', 'soft', 'overlap')),
    derivation_source   TEXT    NOT NULL
                                CHECK (derivation_source IN (
                                    'manual', 'git_ancestry', 'path_overlap', 'oi_ref', 'pr_ref'
                                )),
    confidence          REAL    NOT NULL DEFAULT 1.0,
    evidence_json       TEXT,
    derived_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_track_id, to_track_id)
);

-- ============================================================================
-- TRACK OPEN ITEMS
-- ============================================================================

CREATE TABLE IF NOT EXISTS track_open_items (
    track_id    TEXT    NOT NULL REFERENCES tracks(track_id),
    oi_id       TEXT    NOT NULL,
    link_type   TEXT    NOT NULL CHECK (link_type IN ('blocks', 'warns', 'related')),
    link_source TEXT    NOT NULL CHECK (link_source IN ('file_path', 'mention', 'manual')),
    linked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ============================================================================
-- EXTEND DISPATCHES — rebuild to add CHECK on state + operator_approved_at
-- ============================================================================
--
-- SQLite does not support ALTER TABLE ADD CONSTRAINT or ADD COLUMN with CHECK.
-- Rebuild pattern: rename → create new → copy data → drop old.
--
-- The new state CHECK includes all legacy dispatch states so existing rows
-- remain valid after migration.

ALTER TABLE dispatches RENAME TO dispatches_pre_v22;

CREATE TABLE dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL UNIQUE,
    state           TEXT    NOT NULL DEFAULT 'proposed'
                            CHECK (state IN (
                                'proposed', 'ready', 'active', 'completed', 'failed',
                                'queued', 'claimed', 'delivering', 'accepted', 'running',
                                'timed_out', 'failed_delivery', 'expired', 'recovered',
                                'dead_letter'
                            )),
    terminal_id     TEXT,
    track           TEXT    REFERENCES tracks(track_id),
    priority        TEXT    DEFAULT 'P2',
    pr_ref          TEXT,
    gate            TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    bundle_path     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_after   TEXT,
    metadata_json   TEXT    DEFAULT '{}',
    operator_approved_at TEXT
);

INSERT INTO dispatches (
    id, dispatch_id, state, terminal_id, track, priority, pr_ref, gate,
    attempt_count, bundle_path, created_at, updated_at, expires_after, metadata_json
)
SELECT
    id, dispatch_id, state, terminal_id, track, priority, pr_ref, gate,
    attempt_count, bundle_path, created_at, updated_at, expires_after, metadata_json
FROM dispatches_pre_v22;

DROP TABLE dispatches_pre_v22;

-- ============================================================================
-- INDEXES — dispatches (rebuilt above)
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_dispatch_state
    ON dispatches (state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispatch_terminal
    ON dispatches (terminal_id, state);

CREATE INDEX IF NOT EXISTS idx_dispatch_created
    ON dispatches (created_at DESC);

-- ============================================================================
-- INDEXES — track layer
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_tracks_phase_nextup
    ON tracks(project_id, phase, next_up DESC, sort_order);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up
    ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued';

CREATE INDEX IF NOT EXISTS idx_dispatches_ready
    ON dispatches(state, operator_approved_at)
    WHERE state = 'proposed' OR state = 'ready';

CREATE INDEX IF NOT EXISTS idx_track_deps_from
    ON track_dependencies(from_track_id);

CREATE INDEX IF NOT EXISTS idx_track_phase_history
    ON track_phase_history(track_id, occurred_at DESC);

-- Bump schema version
PRAGMA user_version = 22;
