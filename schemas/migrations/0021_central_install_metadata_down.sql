-- VNX Migration 0021 -- DOWN -- central install metadata rollback
-- Reverses 0021_central_install_metadata.sql: drops central_install_pins
-- and central_install_events tables, rolls back PRAGMA user_version to 20.
--
-- Pre-down state (v21): central_install_pins + central_install_events present.
-- Post-down state (v20): both tables and their indexes dropped.
--
-- Idempotent: DROP TABLE IF EXISTS + DROP INDEX IF EXISTS are safe.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_install_events_project;
DROP INDEX IF EXISTS idx_install_events_type;
DROP TABLE IF EXISTS central_install_events;
DROP TABLE IF EXISTS central_install_pins;

COMMIT;

PRAGMA foreign_keys = ON;

-- PRAGMA user_version must be outside the transaction (SQLite restriction)
PRAGMA user_version = 20;
