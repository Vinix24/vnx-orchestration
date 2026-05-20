-- VNX Migration 2026_05_intelligence_hygiene -- DOWN (quality_intelligence.db only)
-- Reverses 2026_05_intelligence_hygiene.sql: restores valid_until = NULL
-- for patterns that were invalidated by the hygiene filter.
--
-- Pre-down state (v15): governance-event success_patterns and
--   memory_consolidation antipatterns have valid_until set + invalidation_reason stamped.
-- Post-down state (v14): those rows have valid_until = NULL again.
--
-- CAVEAT: This reactivates previously-invalidated patterns (governance noise returns).
-- Idempotent: UPDATE on matching invalidation_reason is safe on repeated runs.
--
-- Apply to: quality_intelligence.db
-- For the runtime_coordination.db rollback (runtime_schema_version), apply:
--   2026_05_intelligence_hygiene_runtime_down.sql

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

UPDATE success_patterns
SET    valid_until         = NULL,
       invalidation_reason = NULL
WHERE  invalidation_reason = 'governance_event_noise_filter_2026_05_hygiene';

UPDATE antipatterns
SET    valid_until         = NULL,
       invalidation_reason = NULL
WHERE  invalidation_reason = 'meta_stats_filter_2026_05_hygiene';

COMMIT;

PRAGMA foreign_keys = ON;
