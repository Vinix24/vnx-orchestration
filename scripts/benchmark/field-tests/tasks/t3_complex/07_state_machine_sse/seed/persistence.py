"""SQLite persistence for ReviewableDocument.

``save_document`` writes the document row and drains the document's pending
audit entries in a single transaction, so the audit row and the state change
are committed atomically (or rolled back together on error).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from state_machine import STATES, ReviewableDocument


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Upsert the document and append any pending audit rows atomically."""
    if doc.state not in STATES:
        raise ValueError(f"document {doc.doc_id} has unknown state '{doc.state}'")

    pending = doc.drain_pending_audit()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO documents (id, project_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id, project_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (doc.doc_id, doc.project_id, doc.state, doc.created_at, doc.updated_at),
            )
            for entry in pending:
                conn.execute(
                    """
                    INSERT INTO state_transitions
                        (document_id, project_id, from_state, to_state,
                         actor, reason, occurred_at)
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
    except sqlite3.Error:
        # Restore pending entries so the caller can retry without losing audit.
        doc._pending_audit = pending + doc._pending_audit
        raise


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument:
    """Load a persisted document. Raises LookupError when no row exists."""
    row = conn.execute(
        """
        SELECT state, created_at, updated_at
        FROM documents
        WHERE id = ? AND project_id = ?
        """,
        (doc_id, project_id),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"no document with id={doc_id} project_id={project_id!r}"
        )
    state, created_at, updated_at = row
    if state not in STATES:
        raise ValueError(
            f"document {doc_id} loaded with unknown state {state!r}"
        )
    doc = ReviewableDocument(doc_id=doc_id, project_id=project_id)
    doc._state = state
    doc.created_at = created_at
    doc.updated_at = updated_at
    doc._pending_audit = []
    return doc


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict[str, Any]]:
    """Return all audit rows for a document in insertion order (oldest first)."""
    cursor = conn.execute(
        """
        SELECT id, document_id, project_id, from_state, to_state,
               actor, reason, occurred_at
        FROM state_transitions
        WHERE document_id = ? AND project_id = ?
        ORDER BY id ASC
        """,
        (doc_id, project_id),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
