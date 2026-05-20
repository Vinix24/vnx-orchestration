-- VNX Migration 2026_05_intelligence_hygiene -- DOWN (runtime_coordination.db only)
-- Removes the runtime_schema_version row inserted by 2026_05_intelligence_hygiene.sql.
--
-- Apply to: runtime_coordination.db
-- Apply this AFTER 2026_05_intelligence_hygiene_down.sql (quality_intelligence.db part).

BEGIN TRANSACTION;

DELETE FROM runtime_schema_version WHERE version = 15;

COMMIT;
