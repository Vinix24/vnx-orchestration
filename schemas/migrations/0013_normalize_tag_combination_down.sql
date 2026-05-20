-- VNX Migration 0013 -- DOWN -- tag_combination normalization rollback (PARTIAL)
-- Reverses 0013_normalize_tag_combination.sql structural change only.
-- Original per-row format (comma-list vs JSON) is not recoverable from SQL alone.
--
-- LIMITATION: Cannot reconstruct which rows were originally comma-lists.
-- A full rollback requires restoring from a pre-0013 database backup.
--
-- What this script does: converts JSON arrays back to comma-list format.
-- Post-down: intelligence_selector.py must use split(",") instead of json_each.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

UPDATE prevention_rules
SET tag_combination = (
    SELECT group_concat(value, ',')
    FROM json_each(prevention_rules.tag_combination)
)
WHERE
    tag_combination IS NOT NULL
    AND trim(tag_combination) != ''
    AND json_valid(tag_combination);

COMMIT;

PRAGMA foreign_keys = ON;
