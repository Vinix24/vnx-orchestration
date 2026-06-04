-- Migration 001 — document_scores table.
--
-- Holds one computed score per (project_id, document_id). ADR-007 binding:
-- every central-DB table carries project_id and a composite UNIQUE over it,
-- so tenants never collide on a bare document_id. Fully idempotent — every
-- statement guards with IF NOT EXISTS, so re-running the file is a no-op.

CREATE TABLE IF NOT EXISTS document_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    score REAL NOT NULL,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Composite UNIQUE is the conflict target for the UPSERT in scorer.py and
-- enforces tenant-scoped uniqueness (ADR-007).
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_scores_project_document
    ON document_scores (project_id, document_id);

-- Supports time-window queries such as "scores computed since T".
CREATE INDEX IF NOT EXISTS idx_document_scores_computed_at
    ON document_scores (computed_at);
