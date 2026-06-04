-- Migration 001: review state-machine tables.
-- Idempotent: every CREATE uses IF NOT EXISTS so the script may run twice.
-- ADR-007 multi-tenant: composite PRIMARY KEY (id, project_id) on documents,
-- and project_id is propagated onto every audit row in state_transitions.

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL,
    project_id   TEXT    NOT NULL,
    from_state   TEXT    NOT NULL,
    to_state     TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    occurred_at  TEXT    NOT NULL,
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents(id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_doc
    ON state_transitions(document_id, project_id, id);
