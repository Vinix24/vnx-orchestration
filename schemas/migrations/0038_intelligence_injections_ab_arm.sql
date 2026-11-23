-- 0038_intelligence_injections_ab_arm.sql — every injection row carries the arm and the window
-- it was served in (track intelligence-effect-measurable, PR-3, decision 20). Without them the
-- effect comparison has no denominator.
--
-- Documents; the runner builds (the auto_apply contract). A paired runner has precedence in
-- auto_apply._apply_one, so this file is never executed as-is and auto_apply.py does not change.
-- scripts/lib/migrations/apply_0038.py checks PRAGMA table_info per column and adds ab_arm TEXT
-- and ab_window TEXT only when they are missing (a store that took the date-named 2026_05 ab_arm
-- migration by hand already has ab_arm), creates the index, stamps user_version 38, all inside
-- BEGIN IMMEDIATE. Both columns are nullable: a row without them is "not assigned".
--
--   ALTER TABLE intelligence_injections ADD COLUMN ab_arm TEXT;      -- 'control' | 'treatment'
--   ALTER TABLE intelligence_injections ADD COLUMN ab_window TEXT;   -- '<window_id>:<config_sha>'
--
-- ADR-007: this adds two columns to an existing table and creates no new central table, so no
-- new UNIQUE or PK is needed (intelligence_injections already has UNIQUE (injection_id, project_id)
-- and ux_intelligence_injections_pid). The new index starts with project_id.
--
-- runtime_schema_version is not touched (the legacy counter stays at 15).
--
-- Down: 0038_intelligence_injections_ab_arm_down.sql, executed by apply_0038.revert_migration.
-- Target DB: runtime_coordination.db · Tested by: tests/test_migration_0038_ab_arm.py

CREATE INDEX IF NOT EXISTS idx_injection_window_arm
    ON intelligence_injections (project_id, ab_window, ab_arm);
