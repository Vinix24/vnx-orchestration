-- 001_state_machine.sql — reviewable-document state machine schema.
-- Idempotent: safe to apply multiple times.
-- ADR-007: every table is multi-tenant; project_id is part of the composite
-- PRIMARY KEY (documents) and stamped + indexed on every audit row
-- (state_transitions).

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (id, project_id)
);

-- Append-only audit trail: one row per state transition.
CREATE TABLE IF NOT EXISTS state_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    project_id  TEXT    NOT NULL,
    from_state  TEXT    NOT NULL,
    to_state    TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    occurred_at TEXT    NOT NULL,
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_doc
    ON state_transitions (project_id, document_id, id);
