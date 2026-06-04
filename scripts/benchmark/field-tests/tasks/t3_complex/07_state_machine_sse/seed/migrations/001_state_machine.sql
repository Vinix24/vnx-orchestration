-- Migration 001 — reviewable-document state machine.
-- Idempotent: re-running this file on an initialised DB is a no-op.
-- Per ADR-007 every persistent table is keyed by a composite that includes
-- project_id, and the audit table references documents via a composite FK.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    state       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (id, project_id),
    CHECK (state IN (
        'DRAFT',
        'PENDING_REVIEW',
        'APPROVED',
        'CHANGES_REQUESTED',
        'REJECTED'
    ))
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL,
    project_id   TEXT    NOT NULL DEFAULT 'default',
    from_state   TEXT    NOT NULL,
    to_state     TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    occurred_at  TEXT    NOT NULL,
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents (id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_doc
    ON state_transitions (document_id, project_id, id);
