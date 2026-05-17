-- Migration: intelligence catalog hygiene
-- Addresses Sonnet audit BLOCKER #2: intelligence +30pp claim is an artefact
-- of catalogue pollution by governance-event and meta-stat noise rows.
--
-- Changes:
--   1. Add invalidation_reason column to success_patterns and antipatterns
--      (idempotent — IF NOT EXISTS, requires SQLite 3.37+)
--   2. Invalidate existing governance-event success_patterns (title LIKE 'gate % passed')
--   3. Invalidate memory_consolidation antipatterns (category = 'memory_consolidation')
--
-- Design: forward-only, no row deletions. Sets valid_until to preserve
--         the audit trail while suppressing rows from selector output
--         (proven_pattern.py and failure_prevention.py both filter valid_until).
--
-- Applied by: operator-run (manual) — not wired into auto_apply.py because
--             this targets quality_intelligence.db, not runtime_coordination.db.
-- Tested by:  tests/test_intelligence_hygiene.py
--
-- Pre-condition: quality_db_init.py has already added valid_until to
--                success_patterns and antipatterns (adds col if missing).
-- Post-migration version: 15

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- ============================================================================
-- 1. Add invalidation_reason column (idempotent)
-- ============================================================================
-- Column addition is handled by the Python bootstrap (quality_db_init.py)
-- via PRAGMA table_info check — SQLite < 3.37 does not support
-- ADD COLUMN IF NOT EXISTS.  This section intentionally left empty.
-- See: codex blocker audit-ih-1-fixforward-20260517-232614

-- ============================================================================
-- 2. Invalidate governance-event noise in success_patterns
--    Targets: "gate <name> passed" titles (81.6% of catalogue, 164/201 rows)
-- ============================================================================

UPDATE success_patterns
SET    valid_until          = datetime('now'),
       invalidation_reason  = 'governance_event_noise_filter_2026_05_hygiene'
WHERE  title LIKE 'gate % passed'
  AND  valid_until IS NULL;

-- ============================================================================
-- 3. Invalidate memory_consolidation noise in antipatterns
--    Targets: category = 'memory_consolidation' (26% of antipattern occurrences)
-- ============================================================================

UPDATE antipatterns
SET    valid_until          = datetime('now'),
       invalidation_reason  = 'meta_stats_filter_2026_05_hygiene'
WHERE  category = 'memory_consolidation'
  AND  valid_until IS NULL;

-- ============================================================================
-- 4. Version stamp
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (15, 'intelligence catalog hygiene: governance-event + memory_consolidation noise filter');

COMMIT;

PRAGMA foreign_keys = ON;
