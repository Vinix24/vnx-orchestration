-- VNX Migration 0014 -- DOWN -- report_findings rollback
-- Reverses 0014_add_report_findings.sql: drops the report_findings table and indexes.
--
-- WARNING: All report_findings data is permanently lost on rollback.
-- Idempotent: DROP TABLE IF EXISTS + DROP INDEX IF EXISTS are safe.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_report_findings_extracted;
DROP INDEX IF EXISTS idx_report_findings_dispatch;
DROP TABLE IF EXISTS report_findings;

COMMIT;

PRAGMA foreign_keys = ON;
