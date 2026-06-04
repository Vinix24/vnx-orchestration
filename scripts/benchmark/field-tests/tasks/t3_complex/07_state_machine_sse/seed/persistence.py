from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Persist document state and flush audit entries to state_transitions."""
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO documents (id, project_id, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (id, project_id) DO UPDATE SET
               state = excluded.state,
               updated_at = excluded.updated_at""",
        (doc.doc_id, doc.project_id, doc.state, now, now),
    )

    for entry in doc.flush_audit():
        conn.execute(
            """INSERT INTO state_transitions
               (document_id, project_id, from_state, to_state, actor, reason, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.doc_id,
                doc.project_id,
                entry["from_state"],
                entry["to_state"],
                entry["actor"],
                entry["reason"],
                now,
            ),
        )


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument | None:
    """Load a document from the database, or return None if not found."""
    row = conn.execute(
        "SELECT id, project_id, state FROM documents WHERE id = ? AND project_id = ?",
        (doc_id, project_id),
    ).fetchone()

    if row is None:
        return None

    doc = ReviewableDocument(doc_id=row[0], project_id=row[1])
    doc.state = row[2]
    return doc


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict]:
    """Return all audit rows for a document, ordered by occurrence."""
    rows = conn.execute(
        """SELECT from_state, to_state, actor, reason, occurred_at
           FROM state_transitions
           WHERE document_id = ? AND project_id = ?
           ORDER BY id ASC""",
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
