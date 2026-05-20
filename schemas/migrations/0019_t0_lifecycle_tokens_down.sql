-- VNX Migration 0019 -- DOWN -- T0 lifecycle tokens rollback
-- Reverses 0019_t0_lifecycle_tokens.sql: removes lease_token column from
-- terminal_leases and drops the partial UNIQUE index.
--
-- Pre-down state (v13): terminal_leases has lease_token TEXT NOT NULL DEFAULT ''
--   + UNIQUE INDEX idx_terminal_leases_token WHERE lease_token != ''.
-- Post-down state (v12): lease_token column and index removed; v13 stamp deleted.
--
-- SQLite 3.35+ required for ALTER TABLE ... DROP COLUMN.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_terminal_leases_token;
ALTER TABLE terminal_leases DROP COLUMN lease_token;
DELETE FROM runtime_schema_version WHERE version = 13;

COMMIT;

PRAGMA foreign_keys = ON;
