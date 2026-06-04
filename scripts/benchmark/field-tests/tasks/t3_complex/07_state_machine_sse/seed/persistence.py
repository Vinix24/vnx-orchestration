"""SQLite persistence for `ReviewableDocument` with composite (id, project_id) keys.

Per ADR-007 every row is stamped with `project_id` and primary/foreign keys
include it. `save_document` writes the document row and any pending audit
rows inside a single transaction — there is no code path that updates state
without also persisting the corresponding audit entry.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument, STATES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Persist `doc` and flush its pending audit rows atomically.

    Uses an explicit BEGIN/COMMIT block so the document UPSERT and every
    queued state_transitions INSERT either all land or none of them do —
    no half-applied state without an audit row.
    """
    if doc.state not in STATES:
        raise ValueError(f"refusing to persist unknown state '{doc.state}'")

    now = _now_iso()
    pending = list(doc.pending_transitions)

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT 1 FROM documents WHERE id = ? AND project_id = ?",
            (doc.doc_id, doc.project_id),
        )
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO documents (id, project_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc.doc_id, doc.project_id, doc.state, now, now),
            )
        else:
            conn.execute(
                "UPDATE documents SET state = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                (doc.state, now, doc.doc_id, doc.project_id),
            )
        for entry in pending:
            conn.execute(
                "INSERT INTO state_transitions "
                "(document_id, project_id, from_state, to_state, actor, reason, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.doc_id,
                    doc.project_id,
                    entry["from_state"],
                    entry["to_state"],
                    entry["actor"],
                    entry["reason"],
                    entry["occurred_at"],
                ),
            )
        conn.execute("COMMIT")
    except sqlite3.DatabaseError:
        conn.execute("ROLLBACK")
        raise

    doc.pending_transitions.clear()


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument:
    row = conn.execute(
        "SELECT state FROM documents WHERE id = ? AND project_id = ?",
        (doc_id, project_id),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"no document with id={doc_id} project_id='{project_id}'"
        )
    doc = ReviewableDocument(doc_id, project_id=project_id)
    doc.state = row[0]
    return doc


def get_audit_trail(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> list[dict]:
    cursor = conn.execute(
        "SELECT id, document_id, project_id, from_state, to_state, "
        "actor, reason, occurred_at "
        "FROM state_transitions "
        "WHERE document_id = ? AND project_id = ? "
        "ORDER BY id ASC",
        (doc_id, project_id),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
