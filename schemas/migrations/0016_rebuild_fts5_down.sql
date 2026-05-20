-- VNX Migration 0016 -- DOWN -- FTS5 project_id rebuild rollback
-- Reverses 0016_rebuild_fts5.sql (Python-driven): removes project_id from
-- code_snippets FTS5 virtual table.
--
-- ARCHITECTURE NOTE: The up-migration is Python-driven via apply_migration_0016()
-- in scripts/migrate_to_central_vnx.py. The down is likewise Python-driven.
-- Pure SQL cannot safely rebuild an FTS5 table with schema-derived column list.
--
-- OI-ROLLBACK-1: Wire --rollback-fts5 into migrate_to_central_vnx.py.
--
-- Manual SQL approach (data loss -- FTS index destroyed; repopulate from source):

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DELETE FROM schema_version WHERE version = '8.4.0-fts5-project-id';

DROP TABLE IF EXISTS code_snippets;

CREATE VIRTUAL TABLE IF NOT EXISTS code_snippets USING fts5(
    title,
    description,
    code,
    file_path,
    line_range,
    tags,
    language,
    framework,
    dependencies,
    quality_score,
    usage_count,
    last_updated,
    tokenize = 'porter unicode61'
);

COMMIT;

PRAGMA foreign_keys = ON;
