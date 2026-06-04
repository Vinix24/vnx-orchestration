"""Persistence layer for ReviewableDocument.

All writes happen inside a single SQLite transaction so the documents row
and the state_transitions audit rows commit atomically — no audit-write-
then-state-change race window.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument


class DocumentNotFound(LookupError):
    """Raised when load_document cannot find (doc_id, project_id)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Upsert the document state and flush all pending audit rows atomically.

    Uses sqlite3's connection-as-context-manager: commits on success, rolls
    back on any exception. State and audit either both land or neither does.
    """
    now = _utcnow_iso()
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
        for record in doc.pending_transitions:
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
                    record.from_state,
                    record.to_state,
                    record.actor,
                    record.reason,
                    record.occurred_at,
                ),
            )
        doc.pending_transitions.clear()


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument:
    row = conn.execute(
        "SELECT state FROM documents WHERE id = ? AND project_id = ?",
        (doc_id, project_id),
    ).fetchone()
    if row is None:
        raise DocumentNotFound(
            f"document id={doc_id} project_id={project_id!r} not found"
        )
    doc = ReviewableDocument(doc_id=doc_id, project_id=project_id)
    doc.state = row[0]
    return doc


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict]:
    cur = conn.execute(
        """
        SELECT id, document_id, project_id, from_state, to_state,
               actor, reason, occurred_at
        FROM state_transitions
        WHERE document_id = ? AND project_id = ?
        ORDER BY id ASC
        """,
        (doc_id, project_id),
    )
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
