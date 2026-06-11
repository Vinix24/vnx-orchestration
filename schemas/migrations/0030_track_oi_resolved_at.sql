-- VNX Migration 0030 — track_open_items: resolved_at + resolution_reason
--
-- Purpose: Non-destructive OI closure. Blocker OIs that are resolved no longer
--          require a manual row-delete. Instead: set resolved_at + resolution_reason
--          and exclude resolved rows from the blocker count in track_reconciler.
--
-- Design: oi-lifecycle-closure feature (ROADMAP 1.0.1)
--         ADR-007 binding: track_open_items already has composite PK
--         (track_id, project_id, oi_id, link_type) — these are additive columns only,
--         no table rebuild, composite discipline preserved.
--
-- Column semantics:
--   resolved_at       TEXT  NULL  = open (NULL = still active blocker)
--                           TEXT  = ISO-8601 timestamp of resolution
--   resolution_reason TEXT  NULL  = operator-supplied reason (recorded at close time)
--
-- Behaviour contract (reconciler):
--   A track_open_items row is treated as a blocker ONLY when:
--     link_type = 'blocks' AND resolved_at IS NULL
--   Rows with resolved_at set are invisible to the blocker path.
--
-- SQLite compatibility: ALTER TABLE ADD COLUMN with NULL default is supported
--   in all SQLite versions. No table rebuild needed.
--
-- Idempotency: apply_script_if_below skips when user_version >= 30.
--   Preflight hook in migrate_future_system.py guards column presence.
--
-- ADR-007 compliance: additive columns only; (track_id, project_id) scoping
--   preserved via existing composite PK — no new table, no composite PK change.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by:  tests/test_track_oi_lifecycle.py

ALTER TABLE track_open_items ADD COLUMN resolved_at TEXT;
ALTER TABLE track_open_items ADD COLUMN resolution_reason TEXT;

-- Index: support efficient "active blockers per track" queries.
-- Partial index (WHERE resolved_at IS NULL) keeps the active-blocker query fast
-- even on large OI tables.
CREATE INDEX IF NOT EXISTS idx_track_oi_active_blockers
    ON track_open_items(track_id, project_id, link_type)
    WHERE resolved_at IS NULL;

PRAGMA user_version = 30;
