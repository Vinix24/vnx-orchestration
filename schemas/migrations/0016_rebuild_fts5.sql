-- VNX Migration 0016 — Phase 6 P4 FTS5 rebuild
-- (Numbered 0016 because 0011-0014 were claimed by intervening migrations.)
--
-- Rebuilds the FTS5 virtual tables in `quality_intelligence.db` so that
-- `project_id` is included in the indexed document. Without this, full-
-- text searches over `code_snippets` and similar virtual tables would
-- return cross-project matches even after the project_id columns are
-- in place on the source tables (FTS5 indexes a snapshot of the document
-- at insert time; column additions on the underlying physical table do
-- not propagate into existing FTS5 indexes).
--
-- The Python runner applies this only after migration 0015 succeeds and
-- only after the per-project import has populated the central DB. It
-- handles the index drop/recreate inside a single transaction; the
-- repopulate step uses an INSERT ... SELECT from the current contents.
--
-- Companion plan: `claudedocs/2026-04-30-single-vnx-migration-plan.md` §4.4.
--
-- Note: the existing schema only declares `code_snippets` as FTS5; other
-- FTS5 indexes (if present in older DBs from out-of-tree extensions) are
-- discovered dynamically by the runner via `SELECT name FROM sqlite_master
-- WHERE type='table' AND sql LIKE '%USING fts5%'`. Hardcoded statements
-- below cover the canonical case.

-- ============================================================================
-- @db: quality_intelligence — code_snippets rebuild
-- ============================================================================

-- Round-4 perf fix: the project_id assignment below uses a correlated
-- subquery against snippet_metadata.snippet_rowid. Without an index on
-- that column the rebuild does a full scan of snippet_metadata for each
-- row in code_snippets_rebuild_tmp (O(N×M)). On a real central with
-- ~855k snippets and ~119k metadata rows that's ~100B row scans /
-- multi-hour runtime. The index turns the lookup into an O(log M) probe
-- and brings the rebuild back to single-digit minutes.
CREATE INDEX IF NOT EXISTS idx_snippet_metadata_rowid
ON snippet_metadata(snippet_rowid);

-- Materialize the existing rows into a temporary table so we can drop
-- and recreate the FTS5 vtab with the project_id column added.
CREATE TABLE IF NOT EXISTS code_snippets_rebuild_tmp AS
SELECT
    rowid,
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
    last_updated
FROM code_snippets;

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
    project_id,
    tokenize = 'porter unicode61'
);

INSERT INTO code_snippets (
    rowid, title, description, code, file_path, line_range, tags,
    language, framework, dependencies, quality_score, usage_count,
    last_updated, project_id
)
SELECT
    rowid, title, description, code, file_path, line_range, tags,
    language, framework, dependencies, quality_score, usage_count,
    last_updated, COALESCE((SELECT project_id FROM snippet_metadata m WHERE m.snippet_rowid = code_snippets_rebuild_tmp.rowid), 'vnx-dev')
FROM code_snippets_rebuild_tmp;

DROP TABLE IF EXISTS code_snippets_rebuild_tmp;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('8.4.0-fts5-project-id', 'Phase 6 P4: rebuild FTS5 virtual tables with project_id column');
