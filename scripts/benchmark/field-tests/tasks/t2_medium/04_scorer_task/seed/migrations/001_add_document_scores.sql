-- 001_add_document_scores.sql
-- Adds the document_scores table that holds per-document computed scores.
-- ADR-007: every central-DB table carries project_id and a composite UNIQUE
-- over (project_id, document_id). Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS document_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    score REAL NOT NULL,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Composite UNIQUE backs the ON CONFLICT(project_id, document_id) UPSERT and
-- enforces one score row per document per project (ADR-007 tenant scoping).
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_scores_project_document
    ON document_scores (project_id, document_id);

-- Time-window queries ("scores computed in the last hour") hit this index.
CREATE INDEX IF NOT EXISTS idx_document_scores_computed_at
    ON document_scores (computed_at);
