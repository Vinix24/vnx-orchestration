-- Migration 0011: Add pattern_category column to success_patterns and antipatterns
--
-- Background (from claudedocs/2026-04-30-intelligence-system-audit.md):
--   54 of 55 successful intelligence injections returned BYTE-IDENTICAL governance
--   gate-pass content ("gate gate_pr0_input_ready_contract passed"). Governance
--   signals were dominating the proven_pattern slot, drowning out actual code
--   patterns.
--
-- This migration introduces a `pattern_category` column to classify patterns by
-- *kind* (code / governance / process / antipattern_evidence) so the selector
-- can apply diversity rules without colliding with the pre-existing `category`
-- column (which carries semantic domain values like "crawler", "storage", etc.).
--
-- Backfill heuristic: titles or descriptions matching the gate-pass shape
--   (^gate ... passed$) → 'governance'. All others default to 'code'.
--
-- Idempotent via ALTER TABLE ... ADD COLUMN guarded by a column check at the
-- application layer (sqlite has no IF NOT EXISTS for ADD COLUMN); the
-- companion migrator (scripts/lib/pattern_dedup.py --apply / migrate_schema)
-- detects existing column before re-running.

-- success_patterns: classify pattern kind
ALTER TABLE success_patterns ADD COLUMN pattern_category TEXT NOT NULL DEFAULT 'code';

-- antipatterns: same classification space (used to mark advisory-only entries)
ALTER TABLE antipatterns ADD COLUMN pattern_category TEXT NOT NULL DEFAULT 'antipattern_evidence';

-- Index for fast diversity filtering
CREATE INDEX IF NOT EXISTS idx_success_patterns_pattern_category
    ON success_patterns (pattern_category, confidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_antipatterns_pattern_category
    ON antipatterns (pattern_category, occurrence_count DESC);

-- Backfill governance heuristic on success_patterns:
--   match title or description starting with "gate" and ending with "passed"
UPDATE success_patterns
SET    pattern_category = 'governance'
WHERE  pattern_category = 'code'
   AND (
            (title       IS NOT NULL AND lower(title)       GLOB 'gate *passed*')
         OR (description IS NOT NULL AND lower(description) GLOB 'gate *passed*')
       );

-- Backfill process heuristic: phrases like "dispatch", "receipt", "gate request"
UPDATE success_patterns
SET    pattern_category = 'process'
WHERE  pattern_category = 'code'
   AND (
            lower(coalesce(title, ''))       GLOB '*dispatch *'
         OR lower(coalesce(title, ''))       GLOB '*receipt *'
         OR lower(coalesce(description, '')) GLOB '*receipt processor*'
       );
