-- 0039_deliverable_dependencies.sql — order between deliverables (ADR-096 §1-2, track
-- blast-radius-en-afhankelijkheden B1). Additive: one new table and one index, no change to
-- an existing table. `kind` hard = the fire is refused (B2), soft = reported only. A removed
-- dependency keeps its row with removed_at and removed_reason, so the trail stays.
--
-- ADR-007 binding: the composite PRIMARY KEY (project_id, deliverable_id, depends_on_id) is
-- the tenant key; the reverse-lookup index starts with project_id as well.
--
-- Executed as-is by scripts/lib/migrations/apply_0039.py inside apply_script_if_below's
-- SAVEPOINT; the runner then checks the v39 manifest shape (a table of this name in another
-- shape makes IF NOT EXISTS a silent no-op) and only then stamps user_version 39.
-- No down file: nothing before 0039 reads the table; on an empty table the way back is
-- DROP TABLE deliverable_dependencies plus PRAGMA user_version = 38.
--
-- Target DB: runtime_coordination.db · Tested by: tests/test_migration_0039_deliverable_dependencies.py

CREATE TABLE IF NOT EXISTS deliverable_dependencies (
    project_id     TEXT NOT NULL,
    deliverable_id TEXT NOT NULL,
    depends_on_id  TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'hard' CHECK (kind IN ('hard', 'soft')),
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    removed_at     TEXT,
    removed_reason TEXT,
    PRIMARY KEY (project_id, deliverable_id, depends_on_id),
    CHECK ((removed_at IS NULL) = (removed_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_deliverable_dependencies_on
    ON deliverable_dependencies (project_id, depends_on_id);
