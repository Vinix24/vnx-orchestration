"""Initialize seed documents.db with 50 rows for the scorer task benchmark.

Run before each cell to give the worker a fresh starting state.

Distribution: 45 published, 3 draft, 1 archived, 1 unknown-status (to test skip).
Word counts span 0..5000 to exercise the clamp at 1000. Ages 0..30 days for
age_decay sensitivity.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def build_seed_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'default'
        )
    """)
    cur.execute("CREATE INDEX idx_documents_project ON documents(project_id)")
    cur.execute("CREATE INDEX idx_documents_status ON documents(status)")

    now = datetime(2026, 6, 4, 9, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(45):
        days_old = i % 30
        wc = (i * 137) % 5000
        rows.append((
            f"Doc {i+1}", f"body of doc {i+1}", wc, "published",
            (now - timedelta(days=days_old)).isoformat(), "default",
        ))
    for i in range(3):
        rows.append((
            f"Draft {i+1}", f"draft body {i+1}", 100 + i * 50, "draft",
            (now - timedelta(days=i * 5)).isoformat(), "default",
        ))
    rows.append((
        "Archived", "archived body", 800, "archived",
        (now - timedelta(days=45)).isoformat(), "default",
    ))
    rows.append((
        "Weird-status", "should be skipped", 500, "queued",
        now.isoformat(), "default",
    ))

    cur.executemany(
        "INSERT INTO documents (title, body, word_count, status, created_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    build_seed_db(here / "documents.db")
    print(f"seed db built at {here / 'documents.db'}")
