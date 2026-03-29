-- VNX Runtime Coordination Schema — Migration v6 (FP-D PR-2)
-- Purpose: Receipt provenance enrichment — bidirectional linkage registry
--          between dispatches, receipts, commits, and PRs.
-- Applies on top of: v5 (FP-D PR-1) — governance evaluation, escalation state
--
-- Design notes:
--   - provenance_registry tracks the bidirectional chain per dispatch
--   - Chain status: complete | incomplete | broken
--   - Gaps stored as JSON array for flexible gap type tracking
--   - One row per dispatch_id (dispatch is the provenance anchor)
--
-- Governance:
--   G-R5: Every committed change traces to a dispatch (registry enforces visibility)
--   G-R6: Receipts are primary evidence (receipt_id links to receipt layer)
--   G-R7: Dispatch, receipt, commit, and PR must be bidirectionally traceable

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- PROVENANCE REGISTRY
-- ============================================================================
-- Tracks the bidirectional provenance chain for each dispatch.
-- One row per dispatch_id — updated as links are discovered.
--
-- Chain status values:
--   complete   — all links (dispatch -> receipt -> commit -> PR) present and valid
--   incomplete — one or more links missing, no contradictions
--   broken     — a link contradicts another (e.g., receipt points to wrong dispatch)
--
-- Invariants:
--   - dispatch_id is the provenance anchor (always present)
--   - Other fields are populated as links are discovered
--   - gaps_json records which links are missing or broken

CREATE TABLE IF NOT EXISTS provenance_registry (
    dispatch_id     TEXT NOT NULL,
    receipt_id      TEXT,
    commit_sha      TEXT,
    pr_number       INTEGER,
    feature_plan_pr TEXT,
    trace_token     TEXT,
    chain_status    TEXT NOT NULL DEFAULT 'incomplete',
    gaps_json       TEXT DEFAULT '[]',
    registered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    verified_at     TEXT,
    verified_by     TEXT,
    PRIMARY KEY (dispatch_id)
);

CREATE INDEX IF NOT EXISTS idx_provenance_chain_status
    ON provenance_registry (chain_status, registered_at DESC);
CREATE INDEX IF NOT EXISTS idx_provenance_commit
    ON provenance_registry (commit_sha);
CREATE INDEX IF NOT EXISTS idx_provenance_pr
    ON provenance_registry (pr_number);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (6, 'FP-D PR-2: receipt provenance enrichment — bidirectional linkage registry');
