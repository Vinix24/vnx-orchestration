-- VNX Migration 0023 — dispatches FK rebuild (adds track → tracks(track_id) constraint)
--
-- Purpose: Add the foreign key from dispatches.track to tracks(track_id).
--   0022 intentionally omitted this FK so that tracks could be seeded and
--   existing dispatch rows tagged before FK enforcement. This migration runs
--   after seed + tag, making the FK safe to add.
--   Option A from dispatch 20260528-fut-1-fix1-codex-r1.
--
-- Pre-condition: tracks table populated (migration 0022 + seed step complete).
--   Orphaned track refs must be nullified before this runs — see
--   _nullify_orphaned_track_refs() in scripts/migrate_future_system.py.
--
-- Idempotency: apply_script_if_below skips the entire script when user_version >= 23.
--   Partial-run safety: SAVEPOINT wraps all statements in apply_script_if_below.
--
-- Applied by: scripts/migrate_future_system.py (step after seed + tag)
-- Tested by: tests/test_migrate_fk_order_real_data.py, tests/test_tracks_schema.py

ALTER TABLE dispatches RENAME TO dispatches_pre_v23;

CREATE TABLE dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL,
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
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
    operator_approved_at TEXT,
    UNIQUE(dispatch_id, project_id)
);

INSERT INTO dispatches (
    id, dispatch_id, project_id, state, terminal_id, track, priority, pr_ref, gate,
    attempt_count, bundle_path, created_at, updated_at, expires_after, metadata_json,
    operator_approved_at
)
SELECT
    id, dispatch_id, project_id, state, terminal_id, track, priority, pr_ref, gate,
    attempt_count, bundle_path, created_at, updated_at, expires_after, metadata_json,
    operator_approved_at
FROM dispatches_pre_v23;

DROP TABLE dispatches_pre_v23;

INSERT OR REPLACE INTO sqlite_sequence(name, seq)
    SELECT 'dispatches', COALESCE(MAX(id), 0) FROM dispatches;

-- Recreate dispatch indexes (dropped with the renamed table)
CREATE INDEX IF NOT EXISTS idx_dispatch_state
    ON dispatches (state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispatch_terminal
    ON dispatches (terminal_id, state);

CREATE INDEX IF NOT EXISTS idx_dispatch_created
    ON dispatches (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispatches_ready
    ON dispatches(state, operator_approved_at)
    WHERE state = 'proposed' OR state = 'ready';

PRAGMA user_version = 23;
