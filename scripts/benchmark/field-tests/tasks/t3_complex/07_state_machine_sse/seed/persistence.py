"""SQLite persistence for ReviewableDocument.

``save_document`` writes the document state and all pending audit entries in
one transaction: either the new state and its audit rows land together, or
neither does. Audit entries are only dropped from the in-memory document
after the commit succeeds, so a failed save can be retried without losing
audit data.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Persist state + pending audit rows atomically (single transaction)."""
    pending = list(doc.pending_audit)
    now = _utc_now()
    with conn:  # one transaction: commit on success, rollback on error
        conn.execute(
            """
            INSERT INTO documents (id, project_id, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (id, project_id) DO UPDATE SET
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (doc.doc_id, doc.project_id, doc.state, now, now),
        )
        conn.executemany(
            """
            INSERT INTO state_transitions
                (document_id, project_id, from_state, to_state,
                 actor, reason, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
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
    doc.mark_persisted(len(pending))


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument:
    """Rehydrate a document from the documents table."""
    row = conn.execute(
        "SELECT state FROM documents WHERE id = ? AND project_id = ?",
        (doc_id, project_id),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"no document with id={doc_id} project_id={project_id!r}"
        )
    return ReviewableDocument.restore(doc_id, project_id, row[0])


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict]:
    """Return all audit rows for a document, oldest first."""
    rows = conn.execute(
        """
        SELECT from_state, to_state, actor, reason, occurred_at
        FROM state_transitions
        WHERE document_id = ? AND project_id = ?
        ORDER BY id
        """,
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
        for from_state, to_state, actor, reason, occurred_at in rows
    ]
