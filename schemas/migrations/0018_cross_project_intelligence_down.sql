-- VNX Migration 0018 -- DOWN -- cross-project intelligence rollback
-- Reverses 0018_cross_project_intelligence.sql: drops global_patterns and
-- cross_project_recommendations tables from global_intelligence.db.
--
-- Pre-down state: global_patterns + cross_project_recommendations present.
-- Post-down state: both tables and their indexes dropped.
--
-- Idempotent: DROP TABLE IF EXISTS + DROP INDEX IF EXISTS are safe.
--
-- NOTE: global_intelligence.db is a SEPARATE database from per-project
-- quality_intelligence.db files. Apply this script only to
-- global_intelligence.db (e.g. ~/.vnx-aggregator/global_intelligence.db).
--
-- Applied by: operator (manual) — sqlite3 ~/.vnx-aggregator/global_intelligence.db < this_file.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_xprec_source;
DROP INDEX IF EXISTS idx_xprec_target;
DROP TABLE IF EXISTS cross_project_recommendations;

DROP INDEX IF EXISTS idx_global_patterns_family;
DROP TABLE IF EXISTS global_patterns;

COMMIT;

PRAGMA foreign_keys = ON;
