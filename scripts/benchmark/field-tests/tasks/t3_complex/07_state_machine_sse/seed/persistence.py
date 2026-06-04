from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument

# Matches migrations/001_state_machine.sql exactly
_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'default',
    state       TEXT    NOT NULL DEFAULT 'DRAFT',
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
    occurred_at TEXT    NOT NULL
);
"""


def apply_migration(conn: sqlite3.Connection) -> None:
    """Apply the state machine schema. Idempotent."""
    conn.executescript(_DDL)


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Persist document state and flush pending audit rows in a single transaction."""
    now = datetime.now(timezone.utc).isoformat()
    pending = list(doc._pending_audit)
    with conn:
        conn.execute(
            """
            INSERT INTO documents (id, project_id, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id, project_id) DO UPDATE SET
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (doc.doc_id, doc.project_id, doc.state, now, now),
        )
        for entry in pending:
            conn.execute(
                """
                INSERT INTO state_transitions
                    (document_id, project_id, from_state, to_state, actor, reason, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.doc_id,
                    doc.project_id,
                    entry.from_state,
                    entry.to_state,
                    entry.actor,
                    entry.reason,
                    entry.occurred_at,
                ),
            )
    doc._pending_audit.clear()


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument:
    row = conn.execute(
        "SELECT state FROM documents WHERE id = ? AND project_id = ?",
        (doc_id, project_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"Document (id={doc_id}, project_id={project_id!r}) not found")
    doc = ReviewableDocument(doc_id=doc_id, project_id=project_id)
    doc.state = row[0]
    return doc


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT from_state, to_state, actor, reason, occurred_at
        FROM state_transitions
        WHERE document_id = ? AND project_id = ?
        ORDER BY id ASC
        """,
        (doc_id, project_id),
    ).fetchall()
    return [
        {
            "from_state": r[0],
            "to_state": r[1],
            "actor": r[2],
            "reason": r[3],
            "occurred_at": r[4],
        }
        for r in rows
    ]
