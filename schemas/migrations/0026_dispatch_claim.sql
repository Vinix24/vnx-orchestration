-- 0026_dispatch_claim.sql
-- N-1 queue claim primitive: columns + index for atomic dispatch claiming.
--
-- Purpose: Add claimed_by + claimed_at provenance columns to dispatches;
--          add composite covering index on (project_id, state, priority,
--          created_at) for the claim_next_queued_dispatch query.
--
-- ADR-007 binding: claim query and claimed_by/claimed_at state are
--          project_id-scoped. A claimer for project A MUST NEVER see
--          project B queued rows.
--          See docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
--
-- Target DB: runtime_coordination.db
-- Applied by: scripts/lib/migrations/apply_0026.py
-- Tested by:  tests/test_claim_next_queued_dispatch.py
--
-- Idempotency: guarded by apply_0026.py — checks runtime_schema_version
--              and column existence before running ALTER TABLE; stamps v15
--              on success. The ALTER TABLE statements in this file are
--              executed only when the Python runner confirms they are safe.
--
-- Pre-migration state  (v14, after 0020): dispatches has no claimed_by/claimed_at.
-- Post-migration state (v15): claimed_by TEXT, claimed_at TEXT, composite index.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- claimed_by: terminal_id that atomically pulled this dispatch from the queue.
-- claimed_at: UTC timestamp of the claim operation.
-- Both are project_id-scoped via the claim query (ADR-007).
ALTER TABLE dispatches ADD COLUMN claimed_by TEXT;
ALTER TABLE dispatches ADD COLUMN claimed_at TEXT;

-- Composite covering index for claim_next_queued_dispatch:
--   SELECT dispatch_id FROM dispatches
--   WHERE project_id = ? AND state = 'queued'
--   ORDER BY priority ASC, created_at ASC LIMIT 1
CREATE INDEX IF NOT EXISTS idx_dispatch_project_state_claim
    ON dispatches(project_id, state, priority, created_at);

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (
    15,
    'N-1 PR-N-1: claimed_by/claimed_at columns + project_state index for atomic queue claim'
);

COMMIT;

PRAGMA foreign_keys = ON;
