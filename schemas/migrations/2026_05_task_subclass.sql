-- 2026_05_task_subclass.sql
-- Performance indexes for scope-based query filtering introduced by fine-grained
-- task_class sub-classification (coding_sql / coding_runtime / coding_intelligence /
-- coding_test / coding_ui). No schema changes -- category column already exists
-- in success_patterns and antipatterns; populate retroactively via:
--   python3 scripts/lib/intelligence_backfill.py [--dry-run]

CREATE INDEX IF NOT EXISTS idx_success_patterns_category ON success_patterns(category);
CREATE INDEX IF NOT EXISTS idx_antipatterns_category ON antipatterns(category);
