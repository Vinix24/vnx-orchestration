-- VNX Migration 0020 — DOWN — elastic worker pool rollback
-- Reverses 0020_elastic_worker_pool.sql: drops pool_config, worker_pools,
-- worker_pool_membership and removes the v14 version stamp.
--
-- Pre-down state (v14): three pool tables + v14 stamp present.
-- Post-down state (v13): pool tables gone; runtime_schema_version back to MAX v13.
--
-- Applied by: scripts/lib/migrations/apply_0020.py --down
-- Tested by:  tests/test_schema_0020_migration.py

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- Drop in FK-dependency order: membership first (references pool_config + terminal_leases),
-- then worker_pools (references pool_config), then pool_config.

DROP INDEX IF EXISTS idx_pool_membership_pool;
DROP INDEX IF EXISTS idx_pool_membership_active;
DROP TABLE IF EXISTS worker_pool_membership;

DROP INDEX IF EXISTS idx_worker_pools_project;
DROP INDEX IF EXISTS idx_worker_pools_state;
DROP TABLE IF EXISTS worker_pools;

DROP INDEX IF EXISTS idx_pool_config_project;
DROP TABLE IF EXISTS pool_config;

DELETE FROM runtime_schema_version WHERE version = 14;

COMMIT;

PRAGMA foreign_keys = ON;
