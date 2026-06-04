-- 001_state_machine.sql — reviewable-document state machine schema.
--
-- Idempotent: re-running this migration is a no-op (CREATE ... IF NOT EXISTS).
-- ADR-007: composite PK (id, project_id) on documents, all child tables
-- stamp project_id so multi-tenant isolation is enforced at the schema level.

CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER NOT NULL,
    project_id TEXT    NOT NULL DEFAULT 'default',
    state      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
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
    occurred_at  TEXT    NOT NULL,
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_doc_project
    ON state_transitions (document_id, project_id, id);
