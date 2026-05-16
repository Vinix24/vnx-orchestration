-- ============================================================
-- MIGRATION: add tenant_id TEXT NOT NULL to events table
-- Target: 50M-row table
-- Runner: scripts/migrate_add_tenant_id_runner.py
-- ============================================================
--
-- Execution order:
--   Phase 0  — create migration_log audit table (idempotent)
--   Phase 1  — ADD COLUMN tenant_id TEXT (nullable, idempotent)
--   Phase 2  — batched backfill (10 000 rows/tx) — executed by runner
--   Phase 3  — NOT NULL enforcement via table reconstruction
--   Rollback — see ROLLBACK section at end of this file
--
-- Concurrency model (SQLite WAL):
--   WAL journal mode allows concurrent readers during all write phases.
--   Phases 1 and 2 hold per-batch short exclusive write locks only.
--   Phase 3 holds an exclusive lock for the duration of the reconstruction;
--   this is unavoidable in SQLite for NOT NULL enforcement on existing columns.
--   For production deployments, schedule Phase 3 during a low-traffic window
--   and set PRAGMA busy_timeout to allow readers to retry transparently.
-- ============================================================


-- ============================================================
-- PHASE 0: Audit table (idempotent)
-- ============================================================

CREATE TABLE IF NOT EXISTS migration_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    migration TEXT    NOT NULL,
    event     TEXT    NOT NULL,          -- start|batch_progress|backfill_complete|not_null_enforced|finish|error|rollback
    detail    TEXT,
    row_count INTEGER,
    ts        TEXT    NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);


-- ============================================================
-- PHASE 1: Add nullable column
-- ============================================================
-- Runner checks column_exists() before executing this statement.
-- If tenant_id already exists this line is skipped, making the
-- migration idempotent even when run twice from scratch.

ALTER TABLE events ADD COLUMN tenant_id TEXT;

-- Audit: runner inserts 'start' event after Phase 1 completes.
-- INSERT INTO migration_log (migration, event, detail)
-- VALUES ('add_tenant_id_to_events_v1', 'start',
--         'batch_size=10000 default_tenant=<arg>');


-- ============================================================
-- PHASE 2: Batched UPDATE template
-- ============================================================
-- Executed by the Python runner in a loop.
-- Each iteration is its own BEGIN/COMMIT transaction (10 000 rows).
-- The WHERE tenant_id IS NULL guard makes each batch idempotent.
-- The runner resumes from MAX(rowid) WHERE tenant_id IS NOT NULL.

-- Template (runner substitutes :default_tenant, :batch_start, :batch_end):
--
-- BEGIN;
-- UPDATE events
-- SET    tenant_id = :default_tenant
-- WHERE  rowid BETWEEN :batch_start AND :batch_end
--   AND  tenant_id IS NULL;
-- COMMIT;
--
-- Audit (every 100 batches):
-- INSERT INTO migration_log (migration, event, detail, row_count)
-- VALUES ('add_tenant_id_to_events_v1', 'batch_progress',
--         'batch=<n> cursor=<rowid> pct=<x.x>', <total_so_far>);


-- ============================================================
-- PHASE 3: NOT NULL enforcement via table reconstruction
-- ============================================================
-- SQLite does not support ALTER COLUMN ... NOT NULL.
-- The standard 12-step pattern is used instead.
-- Runner introspects sqlite_master to build the DDL dynamically.
-- Indexes and triggers on events are recreated after reconstruction.

PRAGMA foreign_keys = OFF;

BEGIN;

-- Runner replaces the CREATE TABLE below with schema introspected at runtime.
-- Shown here as a representative example for a minimal events table:
--
-- CREATE TABLE events_migration_new (
--     id        INTEGER PRIMARY KEY,
--     tenant_id TEXT    NOT NULL,
--     -- ... other columns preserved verbatim from sqlite_master
-- );
--
-- INSERT INTO events_migration_new SELECT * FROM events;
-- DROP TABLE events;
-- ALTER TABLE events_migration_new RENAME TO events;
-- -- Runner also recreates all indexes (SELECT sql FROM sqlite_master
-- --   WHERE type='index' AND tbl_name='events' AND sql IS NOT NULL)

COMMIT;

PRAGMA foreign_keys = ON;

-- Audit: runner inserts after reconstruction:
-- INSERT INTO migration_log (migration, event, detail)
-- VALUES ('add_tenant_id_to_events_v1', 'not_null_enforced',
--         'reconstruction_complete');

-- Audit: runner inserts on successful completion:
-- INSERT INTO migration_log (migration, event, detail, row_count)
-- VALUES ('add_tenant_id_to_events_v1', 'finish',
--         'not_null_enforced=true', <total_rows_migrated>);


-- ============================================================
-- ROLLBACK: undo migration (run as a separate step)
-- ============================================================
-- Usage: python3 scripts/migrate_add_tenant_id_runner.py <db> --rollback
-- Or execute this block directly in sqlite3 CLI.

-- Option A: DROP COLUMN (SQLite 3.35.0+, 2021-03-12)
-- Fastest; no data movement required.
--
-- ALTER TABLE events DROP COLUMN tenant_id;

-- Option B: Table reconstruction (compatible with all SQLite versions)
-- Use when Option A is unavailable or when tenant_id is part of an index
-- that must also be dropped cleanly.
--
-- PRAGMA foreign_keys = OFF;
-- BEGIN;
-- CREATE TABLE events_rollback_tmp AS
--     SELECT [all columns except tenant_id] FROM events;
-- DROP TABLE events;
-- ALTER TABLE events_rollback_tmp RENAME TO events;
-- COMMIT;
-- PRAGMA foreign_keys = ON;

-- Audit: log rollback event
-- INSERT INTO migration_log (migration, event, detail)
-- VALUES ('add_tenant_id_to_events_v1', 'rollback',
--         'tenant_id column removed');
