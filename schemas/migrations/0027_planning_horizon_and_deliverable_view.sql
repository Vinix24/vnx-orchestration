-- VNX Migration 0027 — planning horizon column + deliverables derived view
--
-- Purpose: Phase 1 of the planning layer ("future-state flow", NO-NODE model).
--          Adds the strategic-layer `horizon` field to tracks (now|next|later)
--          and a derived `deliverables` VIEW that rolls dispatches up by their
--          pluggable output identity (output_ref/output_kind already on dispatches).
--
-- Design: claudedocs/FSF-BUILDPLAN-opus.md §7 + §4,
--         claudedocs/PLANNING-OBJECT-MODEL-SYNTHESIS-2026-06-01.md
--
-- Pre-migration state (v26, after 0026): tracks has no `horizon`; dispatches
--          carries output_ref/output_kind (added by vnx_structural_doctor) but
--          no derived rollup view exists.
-- Post-migration state (v27): tracks.horizon TEXT CHECK(now|next|later);
--          deliverables VIEW (GROUP BY project_id, output_ref).
--
-- NO-NODE model: a deliverable is NOT a table. It is the derived GROUP BY over
--          dispatches sharing one output_ref. This view IS the deliverable plane.
--
-- ADR-007 compliance: this migration adds no new central-DB *table*. The view
--          carries project_id in its GROUP BY + projection, so every consumer
--          filters `WHERE project_id = ?` — same composite-scoping contract as
--          the underlying dispatches table (UNIQUE(dispatch_id, project_id)).
--          See docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
--
-- Idempotency: apply_script_if_below skips the entire script when
--          user_version >= 27. ALTER TABLE ADD COLUMN is additive (no rebuild).
--          The view uses CREATE VIEW IF NOT EXISTS. SAVEPOINT wraps all
--          statements in apply_script_if_below.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by:  tests/test_migrate_0027_planning_horizon.py

-- ============================================================================
-- STEP 1: tracks.horizon — strategic-layer scheduling band (now|next|later)
-- ============================================================================
--
-- horizon lives ONLY on the strategic layer (the track), per the 4-architect
-- synthesis. Nullable + no DEFAULT: the seeder backfills it from ROADMAP
-- milestone. CHECK constrains the allowed band values.

ALTER TABLE tracks ADD COLUMN horizon TEXT
    CHECK (horizon IS NULL OR horizon IN ('now', 'next', 'later'));

CREATE INDEX IF NOT EXISTS idx_tracks_horizon
    ON tracks(project_id, horizon, sort_order);

-- ============================================================================
-- STEP 2: deliverables VIEW — derived rollup GROUP BY output_ref
-- ============================================================================
--
-- One row per (project_id, output_ref): the set of dispatches that produce the
-- same pluggable output (pr | post | deal | doc | ...). derived_status is a
-- coarse, computed rollup — never hand-set, never authoritative.

CREATE VIEW IF NOT EXISTS deliverables AS
SELECT
    project_id                                                  AS project_id,
    output_ref                                                  AS deliverable_ref,
    MIN(output_kind)                                            AS output_kind,
    MIN(track)                                                  AS track,
    COUNT(*)                                                    AS dispatch_count,
    SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END)        AS completed_count,
    SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
             THEN 1 ELSE 0 END)                                 AS in_flight_count,
    SUM(CASE WHEN state IN ('proposed', 'ready', 'queued')
             THEN 1 ELSE 0 END)                                 AS planned_count,
    CASE
        WHEN SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'done'
        WHEN SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
                      THEN 1 ELSE 0 END) > 0
            THEN 'in_progress'
        WHEN SUM(CASE WHEN state IN ('failed', 'failed_delivery', 'dead_letter', 'expired', 'timed_out')
                      THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'failed'
        WHEN SUM(CASE WHEN state = 'ready' THEN 1 ELSE 0 END) > 0
            THEN 'ready'
        ELSE 'proposed'
    END                                                         AS derived_status,
    MAX(updated_at)                                             AS last_activity
FROM dispatches
WHERE output_ref IS NOT NULL
GROUP BY project_id, output_ref;

-- Bump schema version
PRAGMA user_version = 27;
