-- VNX Migration 0028 — tracks.derived_status advisory column
--
-- Purpose: Phase 3 of the planning layer — advisory rollup reconciler.
--          Adds derived_status to the tracks table for the computed/advisory
--          status computed by track_reconciler.py.
--
-- Column semantics (ADVISORY ONLY):
--   derived_status TEXT  NULL        = not yet reconciled
--                        'queued'    — no active or terminal dispatches
--                        'in_progress' — dispatches in flight
--                        'blocked'   — blocker OI or unmet dependency
--                        'done'      — all dispatches terminal + PR merged
--                                       (or no pr_ref on track)
--
-- Separation of concerns:
--   tracks.phase (declared status) = authoritative, operator/T0-gated
--   tracks.derived_status          = advisory, reconciler-written, never auto-advances
--
-- NEVER modified by: seeder, roadmap_manager.reconcile(), ROADMAP.yaml
-- ONLY written by:   track_reconciler.reconcile_track / reconcile_all_tracks
--
-- Design: claudedocs/FSF-BUILDPLAN-opus.md §5 (rollup reconciler)
-- ADR-007: column is per-row (track_id, project_id)-scoped; no new table.
--
-- Idempotency: apply_script_if_below skips if user_version >= 28.
--              ALTER TABLE ADD COLUMN is additive; no rebuild needed.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by:  tests/test_track_reconciler.py

ALTER TABLE tracks ADD COLUMN derived_status TEXT;

CREATE INDEX IF NOT EXISTS idx_tracks_derived_status
    ON tracks(project_id, derived_status);

PRAGMA user_version = 28;
