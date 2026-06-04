-- Idempotent migration: reviewable-document state-machine with audit trail
-- Composite PK (id, project_id) per ADR-007

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    state TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (state IN ('DRAFT', 'PENDING_REVIEW', 'APPROVED', 'CHANGES_REQUESTED', 'REJECTED')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (document_id, project_id) REFERENCES documents (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_document
ON state_transitions (document_id, project_id);

CREATE INDEX IF NOT EXISTS idx_state_transitions_occurred_at
ON state_transitions (occurred_at);
