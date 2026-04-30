-- Migration 0012: Add content_hash column to success_patterns for stable pattern_id
--
-- Background (CFX-5 / OI-CFX-5, audit #311 round-2 finding):
--   The intelligence selector derives a per-pattern id ("intel_sp_<row_id>") from
--   the success_patterns primary key. When the same underlying pattern is stored
--   under multiple rows (because pattern_extractor emitted near-identical content
--   on different days / from different dispatches), each row gets its own
--   pattern_id and pattern_usage explodes into N rows for one logical pattern.
--
-- Decision (option B from CFX-5 dispatch — backward compatible):
--   * Compute content_hash = sha256(normalize(title || description))[:16]
--   * Selector resolves item_id by looking up the *canonical* (smallest id) row
--     sharing the same content_hash, so equivalent content collapses onto a
--     single pattern_id without renaming existing intel_sp_<id> values.
--   * pattern_dedup keeps using the column to merge duplicates by content_hash.
--
-- The column is backfilled application-side (SQLite has no SHA-256 builtin)
-- via ``pattern_dedup.backfill_content_hash`` and ``ensure_content_hash_column``.
-- The companion Python helper applies this migration idempotently from test
-- fixtures and the production CLI so a fresh DB never needs manual ALTER.
--
-- Idempotent via column-presence guard at the application layer (sqlite has no
-- IF NOT EXISTS for ADD COLUMN); the runner skips ADD when content_hash already
-- exists.
--
-- Verification (after apply + backfill):
--   sqlite3 quality_intelligence.db \
--     "SELECT COUNT(*) FROM success_patterns WHERE content_hash IS NULL;"
--   -> 0
--   sqlite3 quality_intelligence.db \
--     "SELECT content_hash, COUNT(*) FROM success_patterns
--      GROUP BY content_hash HAVING COUNT(*) > 1;"
--   -> rows requiring pattern_dedup --apply

-- ============================================================================
-- @db: quality_intelligence
-- ============================================================================

ALTER TABLE success_patterns ADD COLUMN content_hash TEXT;

-- Composite index on (content_hash, project_id) for fast canonical lookup.
-- NOT UNIQUE: legacy rows may share a hash before pattern_dedup --apply runs.
-- pattern_dedup enforces application-level uniqueness post-collapse; once the
-- DB is clean, callers can promote this to UNIQUE manually if desired.
CREATE INDEX IF NOT EXISTS idx_success_patterns_content_hash
    ON success_patterns (content_hash, project_id);

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (12, 'Add content_hash column to success_patterns for stable pattern_id (CFX-5)');
