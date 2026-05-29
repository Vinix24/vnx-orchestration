-- VNX Migration 0024 — tracks tenant-scoping (ADR-007 compliance)
--
-- Purpose: Rebuild 4 track tables with composite keys over (track_id, project_id)
--          to support 4-project central VNX install (vnx-dev, seocrawler-v2, mc, salespilot).
--
-- Design: claudedocs/FUT-2-ARCHITECT-DESIGN-2026-05-29.md §2
--
-- Pre-migration state (v22): track tables with globally-unique track_id PKs.
-- Post-migration state (v24): composite PKs + FKs over (track_id, project_id).
--
-- ADR-007 compliance: composite UNIQUE/PK required for all central-DB tables.
-- Kimi peer-review lesson: sqlite_sequence preservation after AUTOINCREMENT rebuilds.
-- Codex peer-review lesson: preflight must assert columns + unique indexes.
-- Fix1 lesson (fut-2a-fix1): child INSERTs derive project_id via JOIN on parent tracks,
--   not hardcoded 'vnx-dev'. Orphan rows (track_id not in tracks) are excluded.
--
-- Idempotency: apply_script_if_below skips entire script when user_version >= 24.
--   Partial-run safety: SAVEPOINT wraps all statements in apply_script_if_below.
--
-- Applied by: scripts/migrate_future_system.py
-- Tested by: tests/test_tracks_v23_schema.py, tests/test_migrate_v23_preserve.py,
--            tests/test_migrate_v24_orphan_handling.py,
--            tests/test_migrate_v24_multi_project_preservation.py

-- ============================================================================
-- STEP 1: REBUILD tracks WITH COMPOSITE PRIMARY KEY (track_id, project_id)
-- ============================================================================
--
-- v22 tracks has: track_id TEXT PRIMARY KEY (single-column), project_id TEXT NOT NULL
-- v24 tracks has: PRIMARY KEY (track_id, project_id) — ADR-007 composite
--
-- Column project_id already present in v22 rows (DEFAULT 'vnx-dev').
-- COALESCE stamps any NULL project_id to 'vnx-dev' for robustness.

ALTER TABLE tracks RENAME TO tracks_pre_v24;

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
    track_id,
    COALESCE(project_id, 'vnx-dev'),
    title, goal_state, phase, next_up, sort_order, priority,
    requires_operator_promotion, instruction_template,
    COALESCE(context_composer_rules, '{}'),
    pr_ref, trigger_condition, created_at, phase_changed_at, completed_at,
    COALESCE(metadata_json, '{}')
FROM tracks_pre_v24;

-- ============================================================================
-- STEP 2: REBUILD track_phase_history WITH project_id + COMPOSITE FK
-- ============================================================================
--
-- v22: id AUTOINCREMENT, track_id TEXT FK, from_phase TEXT, to_phase TEXT, ...
-- v24: adds project_id derived from parent tracks via LEFT JOIN.
--      Composite FK (track_id, project_id) → tracks.
--      Orphan rows (track_id not in tracks) are excluded by WHERE clause.
--
-- sqlite_sequence preservation required (kimi peer-review §1 — AUTOINCREMENT hazard).

ALTER TABLE track_phase_history RENAME TO track_phase_history_pre_v24;

CREATE TABLE track_phase_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    from_phase  TEXT,
    to_phase    TEXT    NOT NULL,
    actor       TEXT    NOT NULL CHECK (actor IN ('operator','T0','system')),
    reason      TEXT,
    approval_id TEXT,
    occurred_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (track_id, project_id) REFERENCES tracks(track_id, project_id)
);

INSERT INTO track_phase_history
    (id, track_id, project_id, from_phase, to_phase, actor, reason, approval_id, occurred_at)
SELECT
    h.id, h.track_id, t.project_id,
    h.from_phase, h.to_phase, h.actor, h.reason, h.approval_id, h.occurred_at
FROM track_phase_history_pre_v24 h
LEFT JOIN tracks t ON t.track_id = h.track_id
WHERE t.track_id IS NOT NULL;

INSERT OR REPLACE INTO sqlite_sequence(name, seq)
    SELECT 'track_phase_history', COALESCE(MAX(id), 0) FROM track_phase_history;

-- ============================================================================
-- STEP 3: REBUILD track_dependencies WITH from/to project_ids + COMPOSITE PK/FK
-- ============================================================================
--
-- v22: (from_track_id, to_track_id) PK, no project_ids
-- v24: (from_track_id, from_project_id, to_track_id, to_project_id) composite PK
--      Both project_ids derived from respective parent tracks via LEFT JOIN.
--      Orphan rows (either track_id not in tracks) are excluded by WHERE clause.
--      Cross-project deps allowed (design §2.4 — JA toelaten)

ALTER TABLE track_dependencies RENAME TO track_dependencies_pre_v24;

CREATE TABLE track_dependencies (
    from_track_id       TEXT    NOT NULL,
    from_project_id     TEXT    NOT NULL DEFAULT 'vnx-dev',
    to_track_id         TEXT    NOT NULL,
    to_project_id       TEXT    NOT NULL DEFAULT 'vnx-dev',
    kind                TEXT    NOT NULL CHECK (kind IN ('hard','soft','overlap')),
    derivation_source   TEXT    NOT NULL
                                CHECK (derivation_source IN (
                                    'manual', 'git_ancestry', 'path_overlap', 'oi_ref', 'pr_ref'
                                )),
    confidence          REAL    NOT NULL DEFAULT 1.0,
    evidence_json       TEXT    DEFAULT '{}',
    derived_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id),
    FOREIGN KEY (from_track_id, from_project_id) REFERENCES tracks(track_id, project_id),
    FOREIGN KEY (to_track_id, to_project_id) REFERENCES tracks(track_id, project_id)
);

INSERT INTO track_dependencies (
    from_track_id, from_project_id, to_track_id, to_project_id,
    kind, derivation_source, confidence, evidence_json, derived_at
)
SELECT
    d.from_track_id, tf.project_id,
    d.to_track_id, tt.project_id,
    d.kind, d.derivation_source, d.confidence,
    COALESCE(d.evidence_json, '{}'), d.derived_at
FROM track_dependencies_pre_v24 d
LEFT JOIN tracks tf ON tf.track_id = d.from_track_id
LEFT JOIN tracks tt ON tt.track_id = d.to_track_id
WHERE tf.track_id IS NOT NULL AND tt.track_id IS NOT NULL;

-- ============================================================================
-- STEP 4: REBUILD track_open_items WITH project_id + COMPOSITE PK/FK
-- ============================================================================
--
-- v22: (track_id, oi_id, link_type) PK, no project_id
-- v24: (track_id, project_id, oi_id, link_type) composite PK
--      project_id derived from parent tracks via LEFT JOIN.
--      Orphan rows (track_id not in tracks) are excluded by WHERE clause.

ALTER TABLE track_open_items RENAME TO track_open_items_pre_v24;

CREATE TABLE track_open_items (
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    oi_id       TEXT    NOT NULL,
    link_type   TEXT    NOT NULL CHECK (link_type IN ('blocks','warns','related')),
    link_source TEXT    NOT NULL CHECK (link_source IN ('file_path','mention','manual')),
    linked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (track_id, project_id, oi_id, link_type),
    FOREIGN KEY (track_id, project_id) REFERENCES tracks(track_id, project_id)
);

INSERT INTO track_open_items
    (track_id, project_id, oi_id, link_type, link_source, linked_at)
SELECT
    h.track_id, t.project_id, h.oi_id, h.link_type, h.link_source, h.linked_at
FROM track_open_items_pre_v24 h
LEFT JOIN tracks t ON t.track_id = h.track_id
WHERE t.track_id IS NOT NULL;

-- ============================================================================
-- STEP 5: DROP renamed pre-v24 tables (clean up)
-- ============================================================================

DROP TABLE track_open_items_pre_v24;
DROP TABLE track_dependencies_pre_v24;
DROP TABLE track_phase_history_pre_v24;
DROP TABLE tracks_pre_v24;

-- ============================================================================
-- STEP 6: INDEXES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_tracks_project_phase_nextup
    ON tracks(project_id, phase, next_up DESC, sort_order);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up_per_project
    ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued';

CREATE INDEX IF NOT EXISTS idx_track_deps_from
    ON track_dependencies(from_track_id, from_project_id);

CREATE INDEX IF NOT EXISTS idx_track_phase_history_track
    ON track_phase_history(track_id, project_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_track_open_items_oi
    ON track_open_items(oi_id);

-- Bump schema version
PRAGMA user_version = 24;
