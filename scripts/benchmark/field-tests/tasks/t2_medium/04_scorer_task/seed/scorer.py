"""Per-document scoring layer.

Separates the pure scoring formula (`compute_score`) from persistence
(`score_document`, `score_all`). The formula is:

    score = base * status_multiplier * age_decay

where base is the word count clamped to [0, 1000] and scaled to [0, 10],
status_multiplier weights the document lifecycle stage, and age_decay damps
older documents. Unknown statuses yield no score and are skipped.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATUS_MULTIPLIERS: dict[str, float] = {
    "draft": 0.5,
    "published": 1.0,
    "archived": 0.25,
}

# Number of times an UPSERT retries when the database is transiently locked.
_LOCK_RETRIES = 3
_LOCK_RETRY_SLEEP = 0.1


def compute_score(word_count: int, status: str, days_since_created: int) -> float | None:
    """Compute a document score, or return None for an unrecognized status.

    base scales clamped word count to a 0-10 range, status_multiplier weights
    the lifecycle stage, and age_decay damps the score for older documents.
    """
    multiplier = STATUS_MULTIPLIERS.get(status)
    if multiplier is None:
        return None

    base = min(word_count, 1000) / 100
    age_decay = 1.0 / (1.0 + days_since_created * 0.05)
    return round(base * multiplier * age_decay, 2)


def _days_since(created_at: str) -> int:
    """Whole days between `created_at` (ISO 8601) and now, floored at 0."""
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    return max(0, delta.days)


def score_document(conn: sqlite3.Connection, document_id: int, project_id: str = "default") -> float | None:
    """Read one document, compute its score, and UPSERT it into document_scores.

    Returns the score, or None if the document is missing or its status is
    unrecognized (in which case nothing is persisted).
    """
    row = conn.execute(
        "SELECT word_count, status, created_at FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        logger.warning("document %s not found; skipping", document_id)
        return None

    word_count, status, created_at = row[0], row[1], row[2]
    score = compute_score(word_count, status, _days_since(created_at))
    if score is None:
        logger.info("document %s has unknown status %r; skipping", document_id, status)
        return None

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(_LOCK_RETRIES):
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO document_scores (document_id, project_id, score)
                    VALUES (?, ?, ?)
                    ON CONFLICT(project_id, document_id)
                    DO UPDATE SET score = excluded.score,
                                  computed_at = CURRENT_TIMESTAMP
                    """,
                    (document_id, project_id, score),
                )
            return score
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_error = exc
            time.sleep(_LOCK_RETRY_SLEEP)

    raise last_error  # type: ignore[misc]


def score_all(conn: sqlite3.Connection, project_id: str = "default") -> dict:
    """Score every document, returning {"scored": N, "skipped": M}.

    A document is skipped when its status is not one of the recognized
    lifecycle stages.
    """
    document_ids = [r[0] for r in conn.execute("SELECT id FROM documents ORDER BY id").fetchall()]

    scored = 0
    skipped = 0
    for document_id in document_ids:
        if score_document(conn, document_id, project_id) is None:
            skipped += 1
        else:
            scored += 1

    return {"scored": scored, "skipped": skipped}
