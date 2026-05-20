-- VNX Migration 2026_05_task_subclass -- DOWN
-- Reverses 2026_05_task_subclass.sql: drops performance indexes for
-- task_class sub-classification scope queries.
--
-- Pre-down state: idx_success_patterns_category + idx_antipatterns_category present.
-- Post-down state: both indexes dropped; category column remains (was pre-existing).
--
-- Idempotent: DROP INDEX IF EXISTS is safe on repeated application.

DROP INDEX IF EXISTS idx_success_patterns_category;
DROP INDEX IF EXISTS idx_antipatterns_category;
