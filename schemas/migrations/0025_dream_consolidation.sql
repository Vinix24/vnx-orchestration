-- 0025_dream_consolidation.sql
-- Add consolidation tracking tables for auto-dream subagent (ADR-019)
--
-- Purpose: Track nightly memory-consolidation cycles run by kimi K2.6 cheap-lane.
--          Provides an audit trail of every dream cycle and an archive record for
--          any pattern that was soft-deleted or merged during consolidation.
--
-- Target DB: quality_intelligence.db (same DB as success_patterns / antipatterns)
-- ADR-007 compliance: all tables carry project_id as composite PK component.
-- ADR-005 compliance: NDJSON events emitted before any write to these tables.
--
-- Applied by: scripts/quality_db_init.py (via apply_script_if_below)
-- Tested by: tests/test_dream_consolidator.py

CREATE TABLE IF NOT EXISTS dream_cycles (
    cycle_id          TEXT    NOT NULL,
    project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
    started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed','failed','reviewed','rejected')),
    provider          TEXT    NOT NULL DEFAULT 'kimi',
    insights_input    INTEGER NOT NULL DEFAULT 0,
    merged_count      INTEGER NOT NULL DEFAULT 0,
    dropped_count     INTEGER NOT NULL DEFAULT 0,
    archived_count    INTEGER NOT NULL DEFAULT 0,
    flagged_count     INTEGER NOT NULL DEFAULT 0,
    operator_reviewed INTEGER NOT NULL DEFAULT 0,
    report_path       TEXT,
    PRIMARY KEY (cycle_id, project_id)  -- ADR-007 composite
);

CREATE TABLE IF NOT EXISTS dream_pattern_archives (
    archive_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id          TEXT    NOT NULL,
    project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
    original_pattern_id INTEGER NOT NULL,
    original_table    TEXT    NOT NULL
                      CHECK (original_table IN ('success_patterns','antipatterns','intelligence_injections')),
    archived_reason   TEXT    NOT NULL
                      CHECK (archived_reason IN ('stale_30d','exact_duplicate','merged_into_other','operator_rejected')),
    archived_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (cycle_id, project_id) REFERENCES dream_cycles(cycle_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_dream_cycles_project_status
    ON dream_cycles(project_id, status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_dream_archives_cycle
    ON dream_pattern_archives(cycle_id, project_id);

-- Pre-initialize sqlite_sequence for dream_pattern_archives (kimi peer-review lesson, FUT-2A).
-- Ensures the AUTOINCREMENT high-water-mark is explicitly tracked from first write.
DELETE FROM sqlite_sequence WHERE name = 'dream_pattern_archives';
INSERT INTO sqlite_sequence(name, seq) VALUES ('dream_pattern_archives', 0);
