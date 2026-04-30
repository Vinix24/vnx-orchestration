-- Migration 0013: Normalize tag_combination column to JSON array format
--
-- Background (OI-CFX-6):
--   The tag_combination column in prevention_rules had two competing formats:
--   - JSON array:  '["architect","Track-C"]'  (written by tag_intelligence.py)
--   - Comma-list:  'architect,Track-C'        (written by learning_loop.py and test seeds)
--
-- Decision: JSON array is canonical — SQLite has json_each, more queryable,
--   future-proof. The reader (intelligence_selector.py) previously used split(",")
--   which silently misparsed JSON array values.
--
-- This migration converts all non-JSON rows to JSON array format.
-- Idempotent: rows already in valid JSON format are skipped via json_valid().
--
-- Handles edge cases:
--   - Single values:      'any'           → '["any"]'
--   - Comma-list:         'architect,T-C' → '["architect","T-C"]'
--   - Space after comma:  'a, b'          → '["a","b"]'
--   - Already JSON array: '["a","b"]'     → unchanged (json_valid = 1)
--   - Empty / NULL:       unchanged

UPDATE prevention_rules
SET tag_combination = (
    '["' ||
    replace(
        replace(trim(tag_combination), ', ', ','),
        ',',
        '","'
    ) ||
    '"]'
)
WHERE
    tag_combination IS NOT NULL
    AND trim(tag_combination) != ''
    AND NOT json_valid(tag_combination);
