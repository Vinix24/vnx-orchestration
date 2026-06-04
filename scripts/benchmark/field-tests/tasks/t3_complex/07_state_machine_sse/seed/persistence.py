"""SQLite persistence for ReviewableDocument and its audit trail."""
from __future__ import annotations

import sqlite3

from state_machine import ReviewableDocument


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Upsert the document row and append every pending audit transition.

    After successful commit, the in-memory audit buffer is cleared so a second
    save does not double-write.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO documents (id, project_id, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id, project_id) DO UPDATE SET
            state = excluded.state,
            updated_at = excluded.updated_at
        """,
        (doc.doc_id, doc.project_id, doc.state, doc.created_at, doc.updated_at),
    )
    for t in doc.pending_transitions:
        cur.execute(
            """
            INSERT INTO state_transitions
                (document_id, project_id, from_state, to_state,
                 actor, reason, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc.doc_id,
                doc.project_id,
                t["from_state"],
                t["to_state"],
                t["actor"],
                t["reason"],
                t["occurred_at"],
            ),
        )
    conn.commit()
    doc.pending_transitions = []


def load_document(
    conn: sqlite3.Connection,
    doc_id: int,
    project_id: str = "default",
) -> ReviewableDocument:
    """Hydrate a ReviewableDocument from its persisted state.

    Raises LookupError if the (doc_id, project_id) pair is not present.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT state, created_at, updated_at
        FROM documents
        WHERE id = ? AND project_id = ?
        """,
        (doc_id, project_id),
    )
    row = cur.fetchone()
    if row is None:
        raise LookupError(
            f"document id={doc_id} project_id={project_id!r} not found"
        )
    doc = ReviewableDocument(doc_id, project_id)
    doc.state = row[0]
    doc.created_at = row[1]
    doc.updated_at = row[2]
    doc.pending_transitions = []
    return doc


def get_audit_trail(
    conn: sqlite3.Connection,
    doc_id: int,
    project_id: str = "default",
) -> list[dict]:
    """Return the audit rows for (doc_id, project_id) in chronological order."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT from_state, to_state, actor, reason, occurred_at
        FROM state_transitions
        WHERE document_id = ? AND project_id = ?
        ORDER BY id ASC
        """,
        (doc_id, project_id),
    )
    return [
        {
            "from_state": r[0],
            "to_state": r[1],
            "actor": r[2],
            "reason": r[3],
            "occurred_at": r[4],
        }
        for r in cur.fetchall()
    ]
