-- VNX Migration 0019 — Wave 5 PR-5.2 T0 lifecycle tokens
-- Purpose: Per-incarnation lease identification via lease_token (UUID v7).
--
-- Design: claudedocs/wave5-pr2-t0-lifecycle-redesign.md §3
-- ADR: ADR-005 (audit-trail invariant), ADR-017 (Control Centre product-shape)
--
-- Pre-migration state (v12, after 0017): composite UNIQUE(terminal_id, project_id)
--                                        on terminal_leases.
--
-- Post-migration state (v13):
--   terminal_leases — adds lease_token TEXT NOT NULL DEFAULT '' column +
--                     UNIQUE INDEX idx_terminal_leases_token WHERE lease_token != ''.
--                     Backfill: existing leased rows get a token so they are
--                     identifiable; released rows keep empty token.
--
-- Atomicity: single BEGIN/COMMIT transaction. FK enforcement suspended.
--            On error, the uncommitted transaction is rolled back when the
--            connection is closed.
--
-- Idempotency: guarded by the Python runner (scripts/lib/migrations/apply_0019.py)
--              which checks runtime_schema_version before executing this script.
--              The final INSERT OR IGNORE is a defence-in-depth guard only.
--
-- Applied by: scripts/lib/migrations/apply_0019.py
-- Tested by:  tests/test_t0_lifecycle.py (via inline applier)

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- 1. terminal_leases: add lease_token column (additive, no table rebuild)
-- ============================================================================

ALTER TABLE terminal_leases ADD COLUMN lease_token TEXT NOT NULL DEFAULT '';

-- ============================================================================
-- 2. Partial UNIQUE index on non-empty tokens
--    Empty string ('') = legacy rows or released rows; collisions allowed.
--    Non-empty token = active per-incarnation identifier; must be globally unique.
-- ============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_leases_token
    ON terminal_leases(lease_token)
    WHERE lease_token != '';

-- ============================================================================
-- 3. Backfill: stamp existing leased rows with a token so they are addressable.
--    16 random hex bytes (32 chars) — forensic-distinct from the v7 format used
--    by the application layer (which has timestamp-prefix structure).
-- ============================================================================

UPDATE terminal_leases
SET lease_token = lower(hex(randomblob(16)))
WHERE state = 'leased' AND lease_token = '';

-- ============================================================================
-- 4. Version stamp
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (13, 'Wave 5 PR-5.2: lease_token for per-incarnation lease identification');

COMMIT;

PRAGMA foreign_keys = ON;
