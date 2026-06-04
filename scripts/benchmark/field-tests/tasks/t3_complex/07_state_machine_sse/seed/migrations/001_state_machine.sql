-- 001_state_machine.sql — idempotent
-- Composite PK (id, project_id) per ADR-007 for multi-tenant isolation

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    state       TEXT    NOT NULL DEFAULT 'DRAFT',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL,
    project_id   TEXT    NOT NULL DEFAULT 'default',
    from_state   TEXT    NOT NULL,
    to_state     TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    occurred_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_document
    ON state_transitions (document_id, project_id);
