-- Migration: add tenant_id to events
-- Target  : SQLite 3.31+ (WAL, instant ADD COLUMN with constant default)
-- Strategy: online, batched, idempotent, resumable
--
-- Why this shape (read before changing):
--   * 50M rows + concurrent writers => no full-table lock acceptable.
--   * SQLite ALTER TABLE cannot add NOT NULL to an existing column without
--     a 12-step table rebuild. A rebuild on 50M rows under live writes is
--     not safe, so NOT NULL is enforced through BEFORE INSERT / UPDATE
--     triggers installed AFTER the backfill completes. Semantically this
--     matches the Postgres pattern requested in the dispatch.
--   * The column is added nullable. ADD COLUMN with no default is
--     metadata-only in SQLite — the table is not rewritten.
--   * Backfill happens in the Python runner; this file only owns DDL,
--     audit table creation, and the per-section rollback statements.
--
-- File layout:
--   @section: prepare     -- audit + resume tables, journal mode
--   @section: forward     -- ADD COLUMN, triggers, post-backfill index
--   @section: rollback    -- companion SQL that undoes everything cleanly
--
-- The runner (migrate_add_tenant_id_runner.py) parses sections by marker
-- and executes only the section relevant to its mode (--direction up|down).

-- @section: prepare ----------------------------------------------------------

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS migration_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    migration   TEXT    NOT NULL,
    event       TEXT    NOT NULL CHECK (event IN ('start','batch','finish','error','rollback_start','rollback_finish')),
    batch_no    INTEGER,
    rows_seen   INTEGER,
    last_rowid  INTEGER,
    message     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_migration_log_migration_event
    ON migration_log(migration, event);

CREATE TABLE IF NOT EXISTS migration_state (
    migration   TEXT PRIMARY KEY,
    last_rowid  INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL CHECK (status IN ('pending','running','complete','rolled_back')) DEFAULT 'pending',
    schema_ver  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT OR IGNORE INTO migration_state(migration, last_rowid, status)
VALUES ('add_tenant_id_to_events', 0, 'pending');

-- @section: forward ----------------------------------------------------------
-- The runner is responsible for executing these because SQLite's
-- ALTER TABLE ADD COLUMN has no IF NOT EXISTS; the runner inspects
-- PRAGMA table_info('events') before issuing it. The statements are
-- preserved here as the canonical DDL for review and replay.
--
-- 1) Add nullable column (metadata-only, instant).
-- ALTER TABLE events ADD COLUMN tenant_id INTEGER;
--
-- 2) The runner performs the batched backfill (10_000 rows per txn).
--
-- 3) Enforce NOT NULL via triggers AFTER backfill completes. Installing
--    these before backfill would fail every concurrent insert during the
--    migration window.
-- CREATE TRIGGER IF NOT EXISTS trg_events_tenant_id_not_null_ins
--     BEFORE INSERT ON events
--     FOR EACH ROW WHEN NEW.tenant_id IS NULL
--     BEGIN SELECT RAISE(ABORT, 'tenant_id must not be NULL'); END;
--
-- CREATE TRIGGER IF NOT EXISTS trg_events_tenant_id_not_null_upd
--     BEFORE UPDATE OF tenant_id ON events
--     FOR EACH ROW WHEN NEW.tenant_id IS NULL
--     BEGIN SELECT RAISE(ABORT, 'tenant_id must not be NULL'); END;
--
-- 4) Optional secondary index. Built last because creating an index over
--    50M rows briefly blocks writers; the runner accepts --build-index
--    to opt in, otherwise the operator schedules a maintenance window.
-- CREATE INDEX IF NOT EXISTS ix_events_tenant_id ON events(tenant_id);

-- @section: rollback ---------------------------------------------------------
-- Undoes the migration cleanly. Idempotent: every statement is guarded
-- so the rollback is safe to re-run after a partial or completed forward
-- run. SQLite has no DROP COLUMN IF EXISTS prior to 3.35; the runner
-- detects the SQLite version and falls back to a table-rebuild when
-- needed (see runner._rollback_drop_column).

DROP TRIGGER IF EXISTS trg_events_tenant_id_not_null_ins;
DROP TRIGGER IF EXISTS trg_events_tenant_id_not_null_upd;
DROP INDEX   IF EXISTS ix_events_tenant_id;

-- The runner issues `ALTER TABLE events DROP COLUMN tenant_id` (SQLite >= 3.35)
-- or a 12-step rebuild for older SQLite, then marks state as rolled_back:
-- UPDATE migration_state
--    SET status='rolled_back',
--        last_rowid=0,
--        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
--  WHERE migration='add_tenant_id_to_events';
