-- Reviewable-document state-machine: documents + append-only audit trail.
-- Idempotent: safe to re-run. Composite PK (id, project_id) per ADR-007.

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    state       TEXT    NOT NULL DEFAULT 'DRAFT'
        CHECK (state IN ('DRAFT', 'PENDING_REVIEW', 'APPROVED',
                         'CHANGES_REQUESTED', 'REJECTED')),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL,
    project_id   TEXT    NOT NULL DEFAULT 'default',
    from_state   TEXT    NOT NULL
        CHECK (from_state IN ('DRAFT', 'PENDING_REVIEW', 'APPROVED',
                              'CHANGES_REQUESTED', 'REJECTED')),
    to_state     TEXT    NOT NULL
        CHECK (to_state IN ('DRAFT', 'PENDING_REVIEW', 'APPROVED',
                            'CHANGES_REQUESTED', 'REJECTED')),
    actor        TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    occurred_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (document_id, project_id)
        REFERENCES documents (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_document
    ON state_transitions (document_id, project_id, id);

CREATE INDEX IF NOT EXISTS idx_state_transitions_occurred_at
    ON state_transitions (occurred_at);
