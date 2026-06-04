"""SQLite persistence for ``ReviewableDocument`` + append-only audit trail.

``save_document`` writes the document row and every buffered audit entry
inside a single ``with conn:`` transaction. Either everything commits or
nothing does — the state row and the audit rows can never diverge on a
crash mid-flush.

Composite ``(id, project_id)`` keys per ADR-007 isolate tenants: two
documents with the same numeric ``id`` but different ``project_id`` are
fully independent rows in both tables.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from state_machine import ReviewableDocument


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_document(conn: sqlite3.Connection, doc: ReviewableDocument) -> None:
    """Upsert state and flush buffered audit entries atomically.

    ``created_at`` is only set on first insert; ``updated_at`` always tracks
    the most recent write. Audit rows preserve the ``occurred_at`` timestamp
    captured by ``ReviewableDocument.transition`` (the moment of the state
    change), not the moment of persistence.
    """
    now = _now_iso()
    pending = doc.drain_pending()

    with conn:
        conn.execute(
            """
            INSERT INTO documents (id, project_id, state, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (id, project_id) DO UPDATE SET
                state      = excluded.state,
                updated_at = excluded.updated_at
            """,
            (doc.doc_id, doc.project_id, doc.state, now, now),
        )

        if pending:
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


def load_document(
    conn: sqlite3.Connection, doc_id: int, project_id: str = "default"
) -> ReviewableDocument | None:
    """Return the document for ``(doc_id, project_id)`` or ``None`` if absent."""
    row = conn.execute(
        "SELECT id, project_id, state FROM documents "
        "WHERE id = ? AND project_id = ?",
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
    """Return audit rows for ``(doc_id, project_id)`` in insertion order."""
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
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "reason": reason,
            "occurred_at": occurred_at,
        }
        for (from_state, to_state, actor, reason, occurred_at) in rows
    ]
