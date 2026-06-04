from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Persist the document's state and flush its pending audit entries.

    The state upsert and every audit insert run inside a single transaction
    (``with conn:``). Either all of it commits or none does, so the persisted
    state can never disagree with the audit trail. ``created_at`` is set only
    on first insert and preserved on update; ``updated_at`` tracks the latest
    write. Each audit row keeps the timestamp captured when the transition
    actually happened, not the moment of persistence.
    """
    now = datetime.now(timezone.utc).isoformat()
    pending = doc.drain_pending()

    with conn:
        conn.execute(
            """INSERT INTO documents (id, project_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (id, project_id) DO UPDATE SET
                   state = excluded.state,
                   updated_at = excluded.updated_at""",
            (doc.doc_id, doc.project_id, doc.state, now, now),
        )

        if pending:
            conn.executemany(
                """INSERT INTO state_transitions
                       (document_id, project_id, from_state, to_state,
                        actor, reason, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        doc.doc_id,
                        doc.project_id,
                        entry["from_state"],
                        entry["to_state"],
                        entry["actor"],
                        entry["reason"],
                        entry["occurred_at"],
                    )
                    for entry in pending
                ],
            )


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument | None:
    """Reconstruct a document from storage, or ``None`` if it does not exist."""
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
    """Return the append-only audit rows for a document in insertion order."""
    rows = conn.execute(
        """SELECT from_state, to_state, actor, reason, occurred_at
               FROM state_transitions
              WHERE document_id = ? AND project_id = ?
           ORDER BY id ASC""",
        (doc_id, project_id),
    ).fetchall()

    return [
        {
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "reason": reason,
            "occurred_at": occurred_at,
        }
        for (from_state, to_state, actor, reason, occurred_at) in rows
    ]
