-- Migration 001 — reviewable-document state-machine.
-- Idempotent: safe to re-run. Per ADR-007, every multitenant table uses a
-- composite key over (id, project_id).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    state       TEXT    NOT NULL CHECK (state IN (
                    'DRAFT', 'PENDING_REVIEW', 'APPROVED',
                    'CHANGES_REQUESTED', 'REJECTED'
                )),
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    from_state  TEXT    NOT NULL,
    to_state    TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    occurred_at TEXT    NOT NULL,
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_doc
    ON state_transitions (document_id, project_id, id);
