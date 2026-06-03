-- VNX Migration 0029 — tracks.track_type + tracks.next_action_owner
--
-- Purpose: VNX 1.0.1 additive columns for the cross-T0 backbone+tracks
--          coordination (business-backbone-SYNTHESE-2026-06-03).
--          VNX owns the generic columns; MC owns client reconcilers +
--          track_events + knowledge_index (separate scope).
--
-- Plan-path: ~/Desktop/BUSINESS/development/mission-control/claudedocs/research/
--            vnx-track-type-spec-input.md
--
-- track_type semantics:
--   coding       = PR/feature lifecycle (planned -> merged -> done)
--   content      = editorial flow (idea -> published -> archived)
--   deal         = commercial pipeline (lead -> won/lost/stalled)
--   relationship = ongoing relation without terminal state (cyclical)
--
-- next_action_owner semantics:
--   me       = Vincent has the ball
--   client   = client/counterpart has the ball (silence = green for deals)
--   waiting  = blocked on third party
--   NULL     = unknown / not applicable
--
-- ADR-007: additive only — no new table, composite PK (track_id, project_id)
--          is preserved, no table rebuild required.
--
-- SQLite compatibility: CHECK on ALTER TABLE ADD COLUMN is supported since
--   SQLite 3.31.0 (2020-01-22) when the CHECK references only the new column.
--   Both constraints here reference only their own column.
--   NOT NULL + DEFAULT 'coding' is safe on ADD COLUMN (DEFAULT fills existing rows).
--
-- Idempotency: apply_script_if_below skips when user_version >= 29.
--   Preflight hook in migrate_future_system.py also guards column presence.
--
-- 1.1 deferred: governance-profile mapping (track_type_profile_map) +
--   GOVERN-path wiring deferred to V3 after V1 selector-activation.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by:  tests/test_migration_track_type.py

-- ============================================================================
-- STEP 1: tracks.track_type — lifecycle discriminator
-- ============================================================================

ALTER TABLE tracks ADD COLUMN track_type TEXT NOT NULL DEFAULT 'coding'
    CHECK (track_type IN ('coding', 'content', 'deal', 'relationship'));

-- ============================================================================
-- STEP 2: tracks.next_action_owner — current ownership signal
-- ============================================================================

ALTER TABLE tracks ADD COLUMN next_action_owner TEXT
    CHECK (next_action_owner IN ('me', 'client', 'waiting') OR next_action_owner IS NULL);

-- ============================================================================
-- STEP 3: index for track_type queries per project
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_tracks_track_type
    ON tracks(project_id, track_type);

PRAGMA user_version = 29;
